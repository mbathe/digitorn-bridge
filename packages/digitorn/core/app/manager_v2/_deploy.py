"""_DeployMixin - full app deployment lifecycle."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypeVar

from digitorn.core.app.compiler import CompiledApp
from digitorn.core.runtime.types import AgentContext, TurnResult

from ._models import DeployedApp, _normalize_scope, _scoped_slug

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# deploys fail in cascade on transient DNS blips; one-second hiccup at boot loses every builtin without retry.

_T = TypeVar("_T")

_DB_TRANSIENT_MARKERS = (
    "getaddrinfo failed",
    "Name or service not known",
    "Temporary failure in name resolution",
    "Connection refused",
    "Connection reset",
    "Connection timed out",
    "could not translate host name",
    "no route to host",
    "network is unreachable",
    "could not connect to server",
)


def _looks_db_transient(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(m.lower() in msg for m in _DB_TRANSIENT_MARKERS)


async def _retry_db_call(
    op: Callable[[], Awaitable[_T]],
    *,
    label: str,
    attempts: int = 4,
    base_delay: float = 0.5,
) -> _T:
    last_exc: BaseException | None = None
    for i in range(1, attempts + 1):
        try:
            return await op()
        except Exception as exc:
            last_exc = exc
            if not _looks_db_transient(exc) or i == attempts:
                raise
            delay = min(base_delay * (2 ** (i - 1)), 10.0)
            logger.warning(
                "db_call_transient_retry label=%s attempt=%d/%d "
                "delay=%.1fs err=%s",
                label, i, attempts, delay, exc,
            )
            await asyncio.sleep(delay)
    # Unreachable; the loop always returns or raises.
    raise last_exc  # type: ignore[misc]


class _DeployMixin:
    """Deployment / lifecycle / resolution methods."""

    _deployed: dict[str, DeployedApp]

    async def deploy(
        self,
        yaml_path: Path,
        *,
        force: bool = False,
        inline_secrets: dict[str, str] | None = None,
        scope: str = "system",
        owner_user_id: str | None = None,
    ) -> DeployedApp:
        """Deploy an app from a YAML file."""
        from digitorn.core.app.yaml_loader import safe_load_strict

        # off-load YAML parse to a thread so an HTTP-triggered deploy doesn't stall the loop; safe_load_strict uses YAML 1.2 bool rules.
        def _read_and_parse() -> dict[str, Any]:
            return safe_load_strict(yaml_path.read_text(encoding="utf-8")) or {}

        raw = await asyncio.to_thread(_read_and_parse)
        peek_app_id = (raw.get("app") or {}).get("app_id", "")
        legacy_secrets: dict[str, str] = {}
        if peek_app_id:
            try:
                legacy_secrets = await self._secret_store.get_all(peek_app_id)
            except Exception as exc:
                logger.warning("Secret store read failed for '%s': %s", peek_app_id, exc, exc_info=True)
        if inline_secrets:
            legacy_secrets.update(inline_secrets)

        # merge legacy per-app secrets with CredentialStore; per-user scopes resolve at runtime, not here.
        try:
            from digitorn.core.credentials.compile_resolver import (
                build_compile_secrets,
            )
            credential_store = getattr(self, "_credential_store", None)
            db_secrets = await build_compile_secrets(
                credential_store,
                app_id=peek_app_id,
                legacy_secrets=legacy_secrets,
            )
        except Exception as exc:
            logger.warning(
                "CredentialStore resolver failed for '%s': %s - "
                "falling back to legacy secrets only",
                peek_app_id, exc,
            )
            db_secrets = legacy_secrets

        compiled = await asyncio.to_thread(
            self._compiler.compile_file,
            yaml_path, secrets=db_secrets or None,
        )
        app_id = compiled.app_id

        for w in (getattr(compiled, "warnings", []) or []):
            logger.warning("compile_warning app=%s: %s", app_id, w)

        async with self._deploy_lock:
            deployed_key = self._deployed_key(
                app_id, scope=scope, owner_user_id=owner_user_id,
            )
            if deployed_key in self._deployed and not force:
                raise RuntimeError(
                    f"App '{app_id}' already deployed at "
                    f"scope={scope!r}. Use force=True to redeploy."
                )

            previous = self._deployed.get(deployed_key)
            logger.info(
                "Deploying app '%s' from %s (scope=%s owner=%s)",
                app_id, yaml_path, scope, owner_user_id,
            )

            if previous is None:
                return await self._build_and_deploy(
                    compiled, scope=scope, owner_user_id=owner_user_id,
                )

            # build the NEW DeployedApp before tearing down the old one so a failed redeploy can roll back atomically.
            self._deployed.pop(deployed_key, None)
            try:
                new_deployed = await self._build_and_deploy(
                    compiled, scope=scope, owner_user_id=owner_user_id,
                )
            except Exception:
                self._deployed[deployed_key] = previous
                logger.warning(
                    "Deploy of '%s' FAILED - rolled back to previous "
                    "deploy to keep existing users online.", app_id,
                )
                raise

            # module-level shutdown only on redeploy; full session teardown stays in undeploy() to keep users online.
            for _mid, _mod in list(getattr(previous, "modules", {}).items()):
                try:
                    await _mod.on_stop()
                except Exception as exc:
                    logger.debug(
                        "previous_deploy_module_on_stop_failed %s.%s: %s",
                        app_id, _mid, exc,
                    )
            if getattr(previous, "context_builder", None) is not None:
                try:
                    await previous.context_builder.on_stop()
                except Exception:
                    logger.debug(
                        "previous_deploy_cb_on_stop_failed", exc_info=True,
                    )
            return new_deployed


    async def run_one_shot(
        self,
        app_id: str,
        user_input: str,
        *,
        user_id: str | None = None,
        on_tool_call: Any | None = None,
    ) -> TurnResult:
        """Run a deployed app in one-shot mode."""
        deployed = self._get_deployed(app_id, user_id=user_id)


        if deployed.mode != "one_shot":
            raise RuntimeError(
                f"App '{app_id}' is in '{deployed.mode}' mode, not 'one_shot'"
            )

        from digitorn.core.runtime.app import RuntimeApp as RuntimeAppExecutor

        executor = RuntimeAppExecutor(
            app_id=app_id,
            execution=deployed.compiled.execution,
            contexts=deployed.contexts,
            modules=deployed.modules,
            context_builder=deployed.context_builder,
            hook_runner=deployed.hook_runner,
        )

        try:
            return await executor.run_one_shot(user_input, on_tool_call=on_tool_call)
        finally:
            pass

    async def get_conversation_executor(
        self,
        app_id: str,
        *,
        user_id: str | None = None,
    ) -> Any:
        """Get a RuntimeApp executor for conversation mode."""
        deployed = self._get_deployed(app_id, user_id=user_id)

        from digitorn.core.runtime.app import RuntimeApp as RuntimeAppExecutor

        return RuntimeAppExecutor(
            app_id=app_id,
            execution=deployed.compiled.execution,
            contexts=deployed.contexts,
            modules=deployed.modules,
            context_builder=deployed.context_builder,
            hook_runner=deployed.hook_runner,
        )

    @staticmethod
    def _deployed_key(
        app_id: str, scope: str = "system",
        owner_user_id: str | None = None,
    ) -> str:
        if scope == "user":
            if not owner_user_id:
                raise ValueError("user scope requires owner_user_id")
            return f"user:{owner_user_id}:{app_id}"
        return f"system::{app_id}"

    def get(
        self,
        app_id: str,
        *,
        user_id: str | None = None,
    ) -> DeployedApp | None:
        """Get a deployed app by ID, resolved for a specific caller."""
        if user_id:
            user_key = self._deployed_key(app_id, "user", user_id)
            hit = self._deployed.get(user_key)
            if hit is not None:
                return hit
        system_key = self._deployed_key(app_id, "system")
        hit = self._deployed.get(system_key)
        if hit is not None:
            return hit
        legacy = self._deployed.get(app_id)
        if legacy is not None:
            return legacy
        # scan any user-scoped deploy as a last resort so session-less callers (diagnostics, admin listings) still find user apps.
        suffix = f":{app_id}"
        for key, app in self._deployed.items():
            if key.endswith(suffix) and key.startswith("user:"):
                return app
        return None

    def list_apps(
        self,
        *,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List deployed apps visible to a caller; user-scoped deploys shadow same-id system deploys."""
        if user_id is None:
            return [app.summary() for app in self._deployed.values()]

        # User-scoped view: filter + shadow
        seen_app_ids: set[str] = set()
        out: list[dict[str, Any]] = []
        # User deploys first so they shadow system ones
        for key, app in self._deployed.items():
            if getattr(app, "scope", "system") == "user":
                if getattr(app, "owner_user_id", None) != user_id:
                    continue
                out.append(app.summary())
                seen_app_ids.add(app.app_id)
        for key, app in self._deployed.items():
            if getattr(app, "scope", "system") != "user":
                if app.app_id in seen_app_ids:
                    continue
                out.append(app.summary())
                seen_app_ids.add(app.app_id)
        return out

    async def list_disabled_apps(
        self,
        *,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a minimal summary of every disabled app from DB."""
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import Application
        from sqlalchemy import or_, select

        try:
            sf = get_session_factory()
        except RuntimeError:
            return []

        async with sf() as session:
            stmt = select(Application).where(Application.disabled == True)  # noqa: E712
            if user_id is not None:
                stmt = stmt.where(
                    or_(
                        Application.scope == "system",
                        (Application.scope == "user") & (Application.owner_user_id == user_id),
                    )
                )
            r = await session.execute(stmt)
            rows = r.scalars().all()

        # `has_bundle` field name is locked in by clients (Re-enable button gates on it); don't rename without a UI release.
        from digitorn.core.packages.resolver import _app_dir
        result = []
        for a in rows:
            owner = a.owner_user_id or None
            install_dir = _app_dir(a.app_id, user_id=owner)
            has_install = (install_dir / "app.yaml").is_file()
            result.append({
                "app_id": a.app_id,
                "scope": a.scope,
                "owner_user_id": a.owner_user_id,
                "name": a.name,
                "version": a.version,
                "disabled": True,
                "disabled_at": a.disabled_at.isoformat() if a.disabled_at else None,
                "disabled_reason": a.disabled_reason or "",
                "has_bundle": has_install,
            })
        return result

    def is_deployed(
        self, app_id: str, *, user_id: str | None = None,
    ) -> bool:
        """Check if an app is deployed (and visible to the caller"""
        return self.get(app_id, user_id=user_id) is not None

    async def undeploy(
        self, app_id: str, *, user_id: str | None = None,
    ) -> bool:
        """Undeploy an app - graceful shutdown of all its modules. Built-in apps cannot be undeployed."""
        if user_id:
            key = self._deployed_key(app_id, "user", user_id)
        else:
            key = self._deployed_key(app_id, "system")
        deployed = self._deployed.get(key)
        if deployed is None:
            deployed = self._deployed.get(app_id)
            if deployed is None:
                return False
            key = app_id
        if getattr(deployed, "builtin", False):
            raise RuntimeError(f"Cannot undeploy built-in app '{app_id}'")
        self._deployed.pop(key, None)

        # stop hot reloader first so it doesn't try to redeploy mid-undeploy.
        if getattr(deployed, "hot_reloader", None) is not None:
            try:
                await deployed.hot_reloader.stop()
            except Exception as exc:
                logger.warning(
                    "hot_reloader_stop_failed app=%s: %s", app_id, exc,
                )

        # Shutdown sandbox: pool or single worker
        if deployed.sandbox_pool is not None:
            try:
                await deployed.sandbox_pool.shutdown()
            except Exception as exc:
                logger.warning("sandbox_pool_shutdown_failed app=%s: %s", app_id, exc)
        if deployed.sandbox_worker is not None:
            try:
                await deployed.sandbox_worker.stop()
            except Exception as exc:
                logger.warning("sandbox_worker_stop_failed app=%s: %s", app_id, exc)

        # Drain: warn about active sessions and cancel pending approvals
        active_keys = [k for k in self._active_sessions if k.startswith(f"{app_id}:")]
        if active_keys:
            logger.warning(
                "Undeploying '%s' with %d active session(s): %s",
                app_id, len(active_keys), active_keys,
            )
        if deployed.approval_queue is not None:
            try:
                deployed.approval_queue.cancel_all()
            except Exception as exc:
                logger.warning("Failed to cancel pending approvals for '%s': %s", app_id, exc, exc_info=True)

        await asyncio.to_thread(self._session_store.delete_for_app, app_id)

        # Clear circuit breaker state for providers used by this app
        from digitorn.core.runtime.agent_loop import clear_circuit_breakers
        provider_ids = set()
        for ctx in deployed.contexts.values():
            pid = getattr(ctx.provider, "provider_id", None)
            if pid:
                provider_ids.add(str(pid))
        if provider_ids:
            clear_circuit_breakers(*provider_ids)

        for module_id, module in deployed.modules.items():
            try:
                await module.on_stop()
            except Exception as exc:
                logger.warning("Module '%s' on_stop failed: %s", module_id, exc, exc_info=True)

        if deployed.context_builder:
            try:
                await deployed.context_builder.on_stop()
            except Exception as exc:
                logger.warning("context_builder on_stop failed: %s", exc, exc_info=True)

        for ctx in deployed.contexts.values():
            if hasattr(ctx, "tool_index"):
                ctx.tool_index = None

        self._llm_channel.unregister_context_builder(app_id)
        try:
            self._scheduler.cancel_jobs_for_app(app_id)
        except Exception as exc:
            logger.warning("scheduler_cancel_jobs_failed app=%s: %s", app_id, exc)
        self._scheduler.unregister_app_executor(app_id)
        self._scheduler.unregister_wake_handler(app_id)

        try:
            await self._channel_registry.stop_and_remove_for_app(app_id)
        except Exception as exc:
            logger.warning("channel_cleanup_failed app=%s: %s", app_id, exc, exc_info=True)

        if self._runtime_store:
            self._runtime_store.unregister(app_id)

        logger.info("App '%s' undeployed", app_id)
        return True

    async def delete_app(
        self,
        app_id: str,
        *,
        user_id: str | None = None,
        scope: str | None = None,
        delete_history: bool = True,
    ) -> dict[str, Any]:
        """Permanently remove a scoped app install - memory, bundles, DB rows, secrets."""
        # Resolve the (scope, owner) tuple once - every step below uses it.
        resolved_scope, resolved_owner = _normalize_scope(user_id, scope)

        # Guard: built-in apps are off-limits (any scope).
        deployed = self.get(app_id, user_id=resolved_owner or None)
        if deployed is not None and getattr(deployed, "builtin", False):
            raise RuntimeError(
                f"Cannot delete built-in app '{app_id}' - "
                f"it will be re-created on the next boot anyway."
            )

        scoped_slug = _scoped_slug(app_id, resolved_scope, resolved_owner)

        result: dict[str, Any] = {
            "app_id": app_id,
            "scope": resolved_scope,
            "owner_user_id": resolved_owner,
            "deployed": False,
            "disk_removed": False,
            "secrets_deleted": 0,
            "db_removed": False,
            "history_preserved": not delete_history,
        }

        # Step 1 - undeploy from memory (scope-aware; idempotent).
        try:
            was_deployed = await self.undeploy(
                app_id, user_id=resolved_owner or None,
            )
            result["deployed"] = bool(was_deployed)
        except RuntimeError:
            raise  # built-in - propagate
        except Exception as exc:
            logger.warning(
                "undeploy failed during delete_app '%s' scope=%s: %s",
                app_id, resolved_scope, exc, exc_info=True,
            )

        try:
            from digitorn.core.database import get_session_factory
            sf = get_session_factory()
        except RuntimeError as exc:
            logger.error(
                "delete_app_db_unavailable app=%s: %s", app_id, exc,
            )
            sf = None

        if sf is not None:
            from sqlalchemy import text as _sql_text
            scope_filter = (
                "app_id = :a AND scope = :s AND owner_user_id = :o"
            )
            params = {
                "a": app_id, "s": resolved_scope, "o": resolved_owner,
            }
            try:
                async with sf() as session:
                    async with session.begin():
                        if delete_history:
                            result_rows = await session.execute(
                                _sql_text(
                                    f"DELETE FROM applications "
                                    f"WHERE {scope_filter}"
                                ),
                                params,
                            )
                            result["db_removed"] = bool(result_rows.rowcount)
                        else:
                            # History-preservation for THIS scope only.
                            from datetime import datetime as _dt, timezone as _tz
                            now = _dt.now(_tz.utc)
                            await session.execute(
                                _sql_text(
                                    f"UPDATE applications "
                                    f"SET disabled = :d, "
                                    f"    disabled_at = :t, "
                                    f"    disabled_reason = :r "
                                    f"WHERE {scope_filter}"
                                ),
                                {
                                    **params,
                                    "d": True,
                                    "t": now,
                                    "r": "preserved_after_delete",
                                },
                            )
                            result["db_removed"] = False
            except Exception as exc:
                logger.error(
                    "DB cleanup failed for '%s' scope=%s owner=%s: %s",
                    app_id, resolved_scope, resolved_owner, exc, exc_info=True,
                )
                raise

        import asyncio as _asyncio
        import shutil
        app_dir = Path.home() / ".digitorn" / "apps" / scoped_slug
        try:
            if app_dir.exists():
                await _asyncio.to_thread(shutil.rmtree, app_dir, False)
                result["disk_removed"] = True
            else:
                result["disk_removed"] = False
        except Exception as exc:
            logger.warning(
                "disk wipe failed for '%s' (%s): %s",
                scoped_slug, app_dir, exc, exc_info=True,
            )

        # Step 4 - purge secrets.
        try:
            secret_keys = await self._secret_store.list_secrets(app_id)
            for k in secret_keys:
                try:
                    await self._secret_store.delete_secret(app_id, k)
                    result["secrets_deleted"] += 1
                except Exception as exc:
                    logger.warning(
                        "secret delete failed app=%s key=%s: %s",
                        app_id, k, exc,
                    )
        except Exception as exc:
            logger.debug("secret listing failed for '%s': %s", app_id, exc)

        logger.info(
            "app_deleted app=%s scope=%s owner=%r deployed=%s "
            "disk=%s secrets=%d db=%s history=%s",
            app_id,
            resolved_scope,
            resolved_owner,
            result["deployed"],
            result["disk_removed"],
            result["secrets_deleted"],
            result["db_removed"],
            "preserved" if result["history_preserved"] else "purged",
        )
        # Truth-check: if absolutely nothing changed on disk, in DB, or
        # in memory, this was a no-op.
        nothing_happened = (
            not result["deployed"]
            and not result["disk_removed"]
            and result["secrets_deleted"] == 0
            and not result["db_removed"]
        )
        result["actually_deleted"] = not nothing_happened
        return result

    async def disable_app(
        self,
        app_id: str,
        *,
        user_id: str | None = None,
        scope: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Disable a scoped app install: undeploy + hide from non-admin list/get."""
        from digitorn.core.database import get_session_factory
        from datetime import datetime as _dt, timezone as _tz
        from sqlalchemy import text as _sql_text

        resolved_scope, resolved_owner = _normalize_scope(user_id, scope)

        deployed = self.get(app_id, user_id=resolved_owner or None)
        if deployed is not None and getattr(deployed, "builtin", False):
            raise RuntimeError(f"Cannot disable built-in app '{app_id}'.")

        was_deployed = False
        try:
            was_deployed = await self.undeploy(
                app_id, user_id=resolved_owner or None,
            )
        except RuntimeError:
            raise
        except Exception as exc:
            logger.warning(
                "undeploy during disable '%s' scope=%s: %s",
                app_id, resolved_scope, exc, exc_info=True,
            )

        try:
            sf = get_session_factory()
        except RuntimeError as exc:
            raise RuntimeError(f"Cannot disable: DB not initialised ({exc})") from exc

        now = _dt.now(_tz.utc)
        async with sf() as session:
            async with session.begin():
                r = await session.execute(
                    _sql_text(
                        "UPDATE applications "
                        "SET disabled = :d, "
                        "    disabled_at = :t, "
                        "    disabled_reason = :r "
                        "WHERE app_id = :a AND scope = :s AND owner_user_id = :o"
                    ),
                    {
                        "d": True,
                        "t": now, "r": reason or "", "a": app_id,
                        "s": resolved_scope, "o": resolved_owner,
                    },
                )
                if r.rowcount == 0:
                    raise RuntimeError(
                        f"App '{app_id}' (scope={resolved_scope}, "
                        f"owner={resolved_owner!r}) not found in DB"
                    )

        logger.info(
            "app_disabled app=%s scope=%s owner=%r reason=%r",
            app_id, resolved_scope, resolved_owner, reason,
        )
        return {
            "app_id": app_id,
            "scope": resolved_scope,
            "owner_user_id": resolved_owner,
            "disabled": True,
            "was_deployed": was_deployed,
        }

    async def enable_app(
        self,
        app_id: str,
        *,
        user_id: str | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Re-enable a disabled scoped install and redeploy it."""
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import Application
        from sqlalchemy import select, text as _sql_text

        resolved_scope, resolved_owner = _normalize_scope(user_id, scope)
        sf = get_session_factory()

        async with sf() as session:
            async with session.begin():
                row = await session.execute(
                    select(Application).where(
                        Application.app_id == app_id,
                        Application.scope == resolved_scope,
                        Application.owner_user_id == resolved_owner,
                    )
                )
                app_row = row.scalar_one_or_none()
                if app_row is None:
                    raise RuntimeError(
                        f"App '{app_id}' (scope={resolved_scope}, "
                        f"owner={resolved_owner!r}) not found"
                    )
                if not app_row.disabled:
                    return {
                        "app_id": app_id,
                        "scope": resolved_scope,
                        "owner_user_id": resolved_owner,
                        "enabled": True,
                        "was_disabled": False,
                    }
                await session.execute(
                    _sql_text(
                        "UPDATE applications "
                        "SET disabled = :d, "
                        "    disabled_at = NULL, "
                        "    disabled_reason = NULL "
                        "WHERE app_id = :a AND scope = :s AND owner_user_id = :o"
                    ),
                    {
                        "d": False,
                        "a": app_id, "s": resolved_scope, "o": resolved_owner,
                    },
                )

        # Redeploy from install_dir on disk.
        redeployed = False
        try:
            install_dir = await self._resolve_install_dir(
                app_id, user_id=resolved_owner or None,
            )
            if install_dir is not None:
                candidate = install_dir / "app.yaml"
                if candidate.is_file():
                    db_secrets: dict[str, str] = {}
                    try:
                        db_secrets = await self._secret_store.get_all(app_id)
                    except Exception as exc:
                        logger.debug("_deploy best-effort block failed: %s", exc)
                    compiled = await asyncio.to_thread(
                        self._compiler.compile_file,
                        candidate, secrets=db_secrets or None,
                    )
                    await self._build_and_deploy(
                        compiled,
                        scope=resolved_scope,
                        owner_user_id=resolved_owner or None,
                    )
                    redeployed = True
        except Exception as exc:
            logger.error(
                "enable_app_redeploy_failed app=%s scope=%s: %s",
                app_id, resolved_scope, exc, exc_info=True,
            )

        logger.info(
            "app_enabled app=%s scope=%s owner=%r redeployed=%s",
            app_id, resolved_scope, resolved_owner, redeployed,
        )
        return {
            "app_id": app_id,
            "scope": resolved_scope,
            "owner_user_id": resolved_owner,
            "enabled": True,
            "was_disabled": True,
            "redeployed": redeployed,
        }

    async def _wipe_user_installs(self, app_id: str) -> None:
        """Remove every USER-scope install of `app_id` - SYSTEM wins."""
        from sqlalchemy import select as _select

        from digitorn.core.database import get_session_factory
        from digitorn.core.models import Application

        try:
            sf = get_session_factory()
        except RuntimeError:
            return

        async def _do_query() -> list[str]:
            async with sf() as session:
                result = await session.execute(
                    _select(Application.owner_user_id).where(
                        Application.app_id == app_id,
                        Application.scope == "user",
                    )
                )
                return [row for row in result.scalars().all() if row]

        try:
            user_owners = await _retry_db_call(
                _do_query, label=f"wipe_user_installs:{app_id}",
            )
        except Exception as exc:
            logger.warning(
                "wipe_user_installs DB query failed after retries app=%s: %s",
                app_id, exc,
            )
            return

        suffix = f":{app_id}"
        for key in list(self._deployed.keys()):
            if not isinstance(key, str) or not key.startswith("user:"):
                continue
            if not key.endswith(suffix):
                continue
            owner = key[len("user:") : -len(suffix)]
            if owner and owner not in user_owners:
                user_owners.append(owner)

        if not user_owners:
            return

        logger.info(
            "system_scope_wipe_user_installs app=%s users=%d",
            app_id, len(user_owners),
        )
        for owner in user_owners:
            try:
                await self.delete_app(
                    app_id,
                    user_id=owner,
                    scope="user",
                    delete_history=False,
                )
            except Exception as exc:
                logger.warning(
                    "system_scope_wipe_user_install_failed "
                    "app=%s owner=%s: %s",
                    app_id, owner, exc, exc_info=True,
                )

    async def reload_app(self, app_id: str) -> dict[str, Any]:
        """Hot-reload a single deployed app from its install_dir."""
        from sqlalchemy import select as _select

        from digitorn.core.database import get_session_factory
        from digitorn.core.models import Application as _Application

        existing = self._deployed.get(app_id)
        if existing is not None and getattr(existing, "builtin", False):
            raise RuntimeError(
                f"Cannot hot-reload built-in app '{app_id}' - "
                f"restart the daemon to pick up changes.",
            )

        _sf = get_session_factory()
        async with _sf() as session:
            result = await session.execute(
                _select(_Application)
                .where(_Application.app_id == app_id)
                .order_by(_Application.scope.asc())
                .limit(1)
            )
            app_row = result.scalars().first()

        if app_row is None:
            raise KeyError(f"App '{app_id}' not found in database.")

        row_owner = getattr(app_row, "owner_user_id", "") or ""
        install_dir = await self._resolve_install_dir(
            app_id, user_id=row_owner or None,
        )

        try:
            current_secrets = await self._secret_store.get_all(app_id)
        except Exception:
            current_secrets = {}

        if install_dir is not None and (install_dir / "app.yaml").is_file():
            compiled = await asyncio.to_thread(
                self._compiler.compile_file,
                install_dir / "app.yaml",
                secrets=current_secrets or None,
            )
            await self.undeploy(app_id, user_id=row_owner or None)
            await self._build_and_deploy(compiled)
            source = "install_dir"
        elif app_row.yaml_content:
            await self._deploy_from_content(
                app_row.yaml_content,
                source=app_row.yaml_path or app_id,
            )
            source = "legacy_yaml_content"
        else:
            raise FileNotFoundError(
                f"App '{app_id}' has no install_dir on disk AND no "
                f"yaml_content. Deploy it again from the source YAML.",
            )

        logger.info(
            "app_reloaded app=%s source=%s secrets=%d",
            app_id, source, len(current_secrets),
        )

        return {
            "app_id": app_id,
            "reloaded": True,
            "secrets_applied": len(current_secrets),
            "source": source,
        }

    async def reload_from_db(self, *, parallelism: int = 16) -> list[str]:
        """Reload all apps from the database at daemon startup."""
        import asyncio as _asyncio

        from sqlalchemy import select

        from digitorn.core.database import _session_factory
        from digitorn.core.models import Application

        if _session_factory is None:
            logger.warning("Cannot reload apps: database not initialized")
            return []

        retry_delays = [2.0, 5.0, 10.0, 20.0]
        apps: list[Any] = []
        last_exc: Exception | None = None
        for attempt, delay in enumerate([0.0, *retry_delays]):
            if delay > 0:
                logger.warning(
                    "reload_from_db: SELECT failed (attempt %d/%d) -- "
                    "retrying in %.1fs after %s: %s",
                    attempt, len(retry_delays) + 1, delay,
                    type(last_exc).__name__ if last_exc else "?",
                    last_exc,
                )
                await _asyncio.sleep(delay)
            try:
                async with _session_factory() as session:
                    result = await session.execute(
                        select(Application)
                    )
                    apps = list(result.scalars().all())
                last_exc = None
                break
            except (TimeoutError, asyncio.TimeoutError, OSError) as exc:
                last_exc = exc
                continue
            except Exception as exc:
                logger.error(
                    "reload_from_db: non-retriable error: %s", exc,
                    exc_info=True,
                )
                raise

        if last_exc is not None:
            logger.error(
                "reload_from_db: exhausted %d retries on transient "
                "TimeoutError. Daemon will boot with NO apps loaded.",
                len(retry_delays) + 1,
            )
            raise last_exc

        if not apps:
            return []

        sem = _asyncio.Semaphore(max(1, int(parallelism)))

        async def _reload_with_sem(app_row: Any) -> str | None:
            async with sem:
                return await self._reload_one_app(app_row)

        results = await _asyncio.gather(
            *(_reload_with_sem(row) for row in apps),
            return_exceptions=True,
        )

        reloaded: list[str] = []
        for app_row, res in zip(apps, results):
            if isinstance(res, BaseException):
                logger.error(
                    "Failed to reload '%s': %s",
                    app_row.app_id, res, exc_info=res,
                )
                continue
            if res:
                reloaded.append(res)

        if reloaded:
            logger.info("Reloaded %d app(s) from DB: %s", len(reloaded), reloaded)

        await self._drop_orphaned_deploys(apps)

        return reloaded

    async def _drop_orphaned_deploys(self, db_apps: list[Any]) -> int:
        """Undeploy in-memory entries that have no matching DB row."""
        wanted: set[str] = set()
        for row in db_apps:
            if getattr(row, "disabled", False):
                continue
            scope = getattr(row, "scope", "system") or "system"
            owner = getattr(row, "owner_user_id", "") or ""
            wanted.add(self._deployed_key(row.app_id, scope, owner))

        orphans: list[tuple[str, str, str | None]] = []
        for key, deployed in list(self._deployed.items()):
            if key in wanted:
                continue
            # Skip builtins: their lifecycle is daemon-owned, not DB-
            # driven (bootstrap_builtins re-deploys them out-of-band).
            if getattr(deployed, "builtin", False):
                continue
            app_id = getattr(deployed, "app_id", None)
            scope = getattr(deployed, "scope", "system") or "system"
            owner = getattr(deployed, "owner_user_id", None) or None
            if app_id:
                orphans.append((app_id, scope, owner))

        if not orphans:
            return 0

        for app_id, scope, owner in orphans:
            try:
                await self.undeploy(app_id, user_id=owner)
                logger.info(
                    "orphan_deploy_dropped app=%s scope=%s owner=%r "
                    "(no matching DB row)",
                    app_id, scope, owner,
                )
            except Exception as exc:
                logger.warning(
                    "orphan_deploy_drop_failed app=%s scope=%s: %s",
                    app_id, scope, exc, exc_info=True,
                )
        return len(orphans)

    async def sync_deployed_with_db(self) -> dict[str, Any]:
        """Reconcile in-memory `_deployed` against the DB."""
        from sqlalchemy import select

        from digitorn.core.database import _session_factory
        from digitorn.core.models import Application

        if _session_factory is None:
            return {"checked": 0, "dropped": 0, "reason": "no_db"}

        async with _session_factory() as session:
            result = await session.execute(select(Application))
            apps = list(result.scalars().all())

        dropped = await self._drop_orphaned_deploys(apps)
        return {
            "checked": len(self._deployed),
            "db_rows": len(apps),
            "dropped": dropped,
        }

    async def _reload_one_app(self, app_row: Any) -> str | None:
        """Reload a single app from its `install_dir` on disk."""
        from sqlalchemy import delete as _delete

        from digitorn.core.database import get_session_factory
        from digitorn.core.models import Application

        app_id = app_row.app_id
        row_scope = getattr(app_row, "scope", "system") or "system"
        row_owner = getattr(app_row, "owner_user_id", "") or ""

        if getattr(app_row, "disabled", False):
            logger.info(
                "reload_skip_disabled app=%s scope=%s owner=%r",
                app_id, row_scope, row_owner,
            )
            return None

        try:
            _install_dir = await self._resolve_install_dir(
                app_id, user_id=row_owner or None,
            )
        except Exception:
            _install_dir = None

        if _install_dir is not None and (_install_dir / "app.yaml").is_file():
            try:
                deployed_key = self._deployed_key(
                    app_id, row_scope, row_owner,
                )
                if deployed_key in self._deployed:
                    await self.undeploy(app_id, user_id=row_owner or None)
                db_secrets: dict[str, str] = {}
                try:
                    db_secrets = await _retry_db_call(
                        lambda: self._secret_store.get_all(app_id),
                        label=f"secrets_get_all:{app_id}",
                    )
                except Exception as exc:
                    logger.warning(
                        "Secret store read failed for '%s': %s",
                        app_id, exc, exc_info=True,
                    )
                compiled = await asyncio.to_thread(
                    self._compiler.compile_file,
                    _install_dir / "app.yaml", secrets=db_secrets or None,
                )
                logger.info(
                    "reload_from_install_dir app=%s scope=%s owner=%r dir=%s",
                    app_id, row_scope, row_owner, _install_dir,
                )
                await self._build_and_deploy(
                    compiled,
                    scope=row_scope,
                    owner_user_id=row_owner or None,
                )
                return app_id
            except Exception as exc:
                logger.warning(
                    "reload_from_install_dir_failed app=%s scope=%s: %s "
                    "- falling back to yaml_content",
                    app_id, row_scope, exc, exc_info=True,
                )

        if app_row.yaml_content:
            logger.info(
                "reload_from_yaml_content app=%s (install_dir missing)",
                app_id,
            )
            try:
                await self._deploy_from_content(
                    app_row.yaml_content,
                    source=app_row.yaml_path or app_id,
                )
                return app_id
            except Exception as exc:
                logger.warning(
                    "reload_from_yaml_content_failed app=%s: %s - "
                    "the cached YAML references files that no longer "
                    "exist (install_dir was wiped). Purging the row.",
                    app_id, exc,
                )
                # fall through to orphan purge

        # Orphan: no install_dir AND (no yaml_content OR yaml_content
        # is unrecoverable) -> purge so the next boot is clean.
        logger.warning(
            "purging_orphan_app app=%s (unrecoverable)",
            app_id,
        )
        try:
            _sf = get_session_factory()
            async with _sf() as _cleanup_session:
                async with _cleanup_session.begin():
                    await _cleanup_session.execute(
                        _delete(Application).where(
                            Application.app_id == app_id,
                        )
                    )
            logger.info("orphan_purged app=%s", app_id)
        except Exception as exc:
            logger.error(
                "failed to purge orphan app '%s': %s",
                app_id, exc, exc_info=True,
            )
        return None

    async def _resolve_install_dir(
        self,
        app_id: str,
        *,
        user_id: str | None = None,
    ) -> "Path | None":
        """Return the on-disk install dir for `app_id`, or None."""
        from digitorn.core.packages.resolver import resolve_app_install_dir

        registry = getattr(self, "_package_registry", None)
        return await resolve_app_install_dir(
            app_id,
            user_id=user_id,
            registry=registry,
        )

    async def _deploy_from_content(
        self, yaml_content: str, *, source: str = "<db>"
    ) -> DeployedApp:
        """Deploy an app from stored YAML content (legacy - no bundle)."""
        from digitorn.core.app.yaml_loader import safe_load_strict

        raw = safe_load_strict(yaml_content)
        peek_app_id = (raw.get("app") or {}).get("app_id", "")
        db_secrets: dict[str, str] = {}
        if peek_app_id:
            try:
                # Retry on transient DB hiccups -- see comment on the
                # other `_secret_store.get_all` call site above.
                db_secrets = await _retry_db_call(
                    lambda: self._secret_store.get_all(peek_app_id),
                    label=f"secrets_get_all:{peek_app_id}",
                )
            except Exception as exc:
                logger.warning("Secret store read failed for '%s': %s", peek_app_id, exc, exc_info=True)
        compiled: CompiledApp | None = None
        if peek_app_id:
            try:
                install_dir = await self._resolve_install_dir(peek_app_id)
            except Exception as exc:
                logger.debug(
                    "install_dir lookup failed for '%s': %s",
                    peek_app_id, exc,
                )
                install_dir = None
            if install_dir is not None:
                candidate = install_dir / "app.yaml"
                if candidate.is_file():
                    try:
                        compiled = await asyncio.to_thread(
                            self._compiler.compile_file,
                            candidate, secrets=db_secrets or None,
                        )
                        logger.info(
                            "Reload promoted from yaml_content to compile_file "
                            "for '%s' (install_dir=%s)",
                            peek_app_id, install_dir,
                        )
                    except Exception as exc:
                        logger.warning(
                            "compile_file from install_dir failed for '%s', "
                            "falling back to compile_string: %s",
                            peek_app_id, exc,
                        )
                        compiled = None

        if compiled is None:
            compiled = self._compiler.compile_string(
                yaml_content, source=source, secrets=db_secrets or None,
            )
        app_id = compiled.app_id

        if app_id in self._deployed:
            await self.undeploy(app_id)

        logger.info("Deploying app '%s' from stored YAML content", app_id)
        return await self._build_and_deploy(compiled)


    @property
    def _sandbox_enabled(self) -> bool:
        settings = getattr(self, "_settings", None)
        if settings and hasattr(settings, "server"):
            return getattr(settings.server, "sandbox", False)
        return False

    def _should_sandbox(self, compiled: CompiledApp) -> bool:
        if not self._sandbox_enabled or compiled.security_profile is None:
            return False
        return True

    def _should_use_pool(self, compiled: CompiledApp) -> bool:
        """Check if this app needs a WorkerPool (per-session sandbox)."""
        ws_mode = getattr(compiled.execution, "workspace_mode", "auto")
        sandbox_cfg = self._get_sandbox_config(compiled)
        if ws_mode == "required":
            return True  # Must use pool - Landlock can't change workspace
        if sandbox_cfg is not None:
            level = getattr(sandbox_cfg, "level", "standard")
            if level in ("strict", "maximum"):
                return True
        return False

    @staticmethod
    def _get_sandbox_config(compiled: CompiledApp) -> Any:
        return getattr(compiled.execution, "sandbox", None)

    async def _build_and_deploy(
        self,
        compiled: CompiledApp,
        *,
        scope: str = "system",
        owner_user_id: str | None = None,
    ) -> DeployedApp:
        """Single bootstrap path for all deploy methods."""
        app_id = compiled.app_id

        if scope == "system":
            await self._wipe_user_installs(app_id)

        from digitorn.core.runtime.bootstrap import bootstrap as build_agent_contexts

        try:
            from digitorn.core.credentials.inject_deploy_time import (
                inject_deploy_time_credentials,
            )
            credential_store = getattr(self, "_credential_store", None)
            credential_audit = getattr(self, "_credential_audit", None)
            if credential_store is not None:
                injected = await inject_deploy_time_credentials(
                    compiled,
                    store=credential_store,
                    audit=credential_audit,
                )
                if injected:
                    logger.info(
                        "credentials_injected_at_deploy app=%s count=%d",
                        app_id, len(injected),
                    )
        except Exception as exc:
            from digitorn.core.credentials.injector import (
                CredentialInjectError as _CIE,
            )
            if isinstance(exc, _CIE):
                raise RuntimeError(
                    f"Credential injection failed for '{app_id}': {exc}"
                ) from exc
            logger.warning(
                "deploy_time_credential_injection_skipped app=%s: %s",
                app_id, exc,
            )

        try:
            _skip_emb = False
            try:
                from digitorn.core.config import get_settings
                _skip_emb = get_settings().discovery.skip_embeddings
            except Exception as exc:
                logger.debug("_deploy best-effort block failed: %s", exc)
            agent_result = await build_agent_contexts(compiled, self._registry, skip_embeddings=_skip_emb)
        except Exception as exc:
            raise RuntimeError(
                f"Agent context build failed for '{app_id}': {exc}"
            ) from exc

        if self._runtime_store is not None:
            try:
                self._runtime_store.register(compiled)
            except Exception as exc:
                logger.warning("Runtime store registration failed: %s", exc, exc_info=True)

        try:
            from digitorn.core.app.syncer import AppSyncer

            syncer = AppSyncer()
            synced = await syncer.sync(
                compiled,
                scope=scope,
                owner_user_id=owner_user_id or "",
            )
            if synced:
                logger.debug("app_db_synced: %s", app_id)
        except Exception as exc:
            logger.warning("app_db_sync_failed: %s - %s", app_id, exc, exc_info=True)

        channels_created = 0
        for name, ch_compiled in compiled.channels.items():
            try:
                instance = self._channel_registry.create_instance(
                    name, ch_compiled.channel_type, ch_compiled.config,
                    app_id=app_id,
                    resolver_config=ch_compiled.user_resolver,
                )
                await self._channel_registry.start_instance(name)
                channels_created += 1
            except Exception as exc:
                logger.warning(
                    "channel_create_failed: %s (type=%s) - %s",
                    name, ch_compiled.channel_type, exc, exc_info=True,
                )

        cb = agent_result["context_builder"]
        if cb is not None and hasattr(cb, "set_job_store"):
            try:
                cb.set_job_store(self._job_store)
                cb._app_id = app_id
                cb._scheduler = self._scheduler
                cb._channel_registry = self._channel_registry
                cb._app_manager = self
                self._llm_channel.register_context_builder(app_id, cb)
                self._scheduler.register_app_executor(app_id, cb)
                self._register_wake_handler(app_id)
            except Exception as exc:
                logger.warning("app_service_wiring_failed app=%s: %s", app_id, exc, exc_info=True)

        app_modules = agent_result.get("modules", {})
        for name in compiled.channels:
            try:
                if app_modules:
                    self._channel_registry.set_resolver_modules(name, app_modules)
                self._channel_registry.set_resolver_user_store(name, self._user_store)
            except Exception as exc:
                logger.warning("channel_resolver_failed app=%s channel=%s: %s", app_id, name, exc)

        mcp_module = app_modules.get("mcp")
        if mcp_module is not None:
            try:
                mcp_module._user_store = self._user_store
                mcp_module._app_id = app_id
                if hasattr(self, "_daemon_mcp_pool") and self._daemon_mcp_pool is not None:
                    mcp_module._daemon_pool = self._daemon_mcp_pool

                mcp_config = compiled.modules.get("mcp")
                if mcp_config is not None and mcp_config.config:
                    try:
                        await mcp_module.on_config_update(mcp_config.config)
                    except Exception as exc:
                        logger.warning(
                            "mcp_reconnect_after_inject app=%s: %s", app_id, exc, exc_info=True,
                        )

                try:
                    await mcp_module._preload_oauth_tokens()
                except Exception as exc:
                    logger.warning(
                        "mcp_oauth_preload_failed app=%s: %s", app_id, exc, exc_info=True,
                    )
            except Exception as exc:
                logger.warning("mcp_setup_failed app=%s: %s", app_id, exc, exc_info=True)

            if cb is not None:
                try:
                    old_count = cb.index.total_tools if cb.index else 0
                    security_profile = getattr(compiled, "security_profile", None)
                    new_index = await asyncio.to_thread(
                        cb.build_and_set_index,
                        app_modules, security_profile,
                    )
                    new_count = new_index.total_tools if new_index else 0
                    if new_count > old_count:
                        self._refresh_agent_tools(
                            compiled, agent_result, cb, new_index,
                        )
                        logger.info(
                            "tool_index_rebuilt_after_preload app=%s tools=%d→%d",
                            app_id, old_count, new_count,
                        )
                except Exception as exc:
                    logger.warning("tool_index_rebuild_failed app=%s: %s", app_id, exc, exc_info=True)

        deployed = DeployedApp(
            app_id=app_id,
            compiled=compiled,
            contexts=agent_result["contexts"],
            modules=agent_result["modules"],
            context_builder=cb,
            bootstrap_result=None,
            hook_runner=agent_result.get("hook_runner"),
            approval_queue=agent_result.get("approval_queue"),
            scope=scope,
            owner_user_id=owner_user_id,
        )
        try:
            for _agent_ctx in agent_result["contexts"].values():
                setattr(_agent_ctx, "event_bus", self.event_bus)
        except Exception:
            logger.debug("event_bus_attach_to_context_failed", exc_info=True)
        deployed_key = self._deployed_key(
            app_id, scope=scope, owner_user_id=owner_user_id,
        )
        self._deployed[deployed_key] = deployed

        if deployed.approval_queue is not None:
            try:
                deployed.approval_queue._app_id = app_id
                deployed.approval_queue.add_on_request(
                    self._make_approval_publisher(app_id),
                )
                deployed.approval_queue.add_on_resolve(
                    self._approval_resolve_publisher(app_id),
                )
            except Exception as exc:
                logger.warning(
                    "approval_publisher_wire_failed app=%s: %s", app_id, exc,
                )

        for mod_name in ("preview", "widget"):
            mod = app_modules.get(mod_name)
            if mod is not None and hasattr(mod, "_event_bus"):
                mod._event_bus = self.event_bus
                mod._bus_app_id = app_id
                logger.info(
                    "bus_wired module=%s app=%s sio=%s",
                    mod_name, app_id, self.event_bus._sio is not None,
                )

        if cb is not None and hasattr(cb, "_on_notification_relay"):
            _bus = self.event_bus
            _aid = app_id

            # Map internal memory event types to frontend action names
            _MEMORY_EVENT_MAP = {
                "todo_added": "add_todo",
                "todo_updated": "update_todo",
                "goal_set": "set_goal",
                "fact_added": "remember",
                "fact_removed": "forget",
            }

            # Map internal agent event types to frontend action names
            _AGENT_EVENT_MAP = {
                "agent_spawn": "spawn_agent",
                "agent_progress": "agent_progress",
                "agent_completed": "agent_result",
                "agent_failed": "agent_result",
                "agent_timeout": "agent_result",
                "agent_cancelled": "agent_result",
                "agent_cancel": "agent_cancel",
                "agent_retrying": "agent_progress",
            }

            def _relay(session_id: str, notification: dict) -> None:
                try:
                    import asyncio as _aio
                    loop = _aio.get_running_loop()
                    bus_key = _bus.session_key(_aid, session_id)
                except RuntimeError:
                    return  # No event loop - standalone CLI mode

                # Route by type first - agent events have "type" starting with "agent_"
                event_type = notification.get("type", "")

                # Background task events (shell, context_builder bg tasks)
                # Discriminate: bg tasks have task_id+tool_name, agent events have agent_id
                status = notification.get("status")
                from digitorn.core.events.envelope import (
                    SessionEvent, OpType, OpState, gen_op_id,
                )
                _uid_for_event = notification.get("user_id") or "system"
                if (
                    status in ("progress", "completed", "failed", "cancelled")
                    and not event_type.startswith("agent_")
                    and notification.get("task_id")
                ):
                    task_id = notification.get("task_id") or gen_op_id("bg")
                    _state_map = {
                        "progress": OpState.RUNNING,
                        "completed": OpState.COMPLETED,
                        "failed": OpState.FAILED,
                        "cancelled": OpState.CANCELLED,
                    }
                    loop.create_task(_bus.emit(SessionEvent.build(
                        type="bg_task_update",
                        app_id=_aid,
                        session_id=session_id,
                        user_id=_uid_for_event,
                        op_id=task_id,
                        op_type=OpType.TOOL,
                        op_state=_state_map[status],
                        payload={
                            "task_id": task_id,
                            "tool_name": notification.get("tool_name"),
                            "status": status,
                            "elapsed_seconds": notification.get("elapsed_seconds", 0),
                            "result_preview": notification.get("result_preview", "")[:500],
                            "hint": notification.get("hint", ""),
                            "error": notification.get("error", ""),
                        },
                    )))
                    return
                agent_action = _AGENT_EVENT_MAP.get(event_type)
                if agent_action is not None:
                    agent_payload = dict(notification)
                    agent_payload["action"] = agent_action
                    agent_payload.pop("type", None)
                    agent_id = (
                        notification.get("agent_id")
                        or gen_op_id("agent")
                    )
                    parent_agent = notification.get("parent_agent")
                    # Map the internal status → contract OpState.
                    _agent_state_map = {
                        "agent_spawn": OpState.RUNNING,
                        "agent_progress": OpState.RUNNING,
                        "agent_retrying": OpState.RUNNING,
                        "agent_completed": OpState.COMPLETED,
                        "agent_failed": OpState.FAILED,
                        "agent_timeout": OpState.TIMEOUT,
                        "agent_cancelled": OpState.CANCELLED,
                        "agent_cancel": OpState.CANCELLED,
                    }
                    op_state = _agent_state_map.get(event_type, OpState.RUNNING)
                    agent_payload["op_id"] = agent_id
                    loop.create_task(_bus.emit(SessionEvent.build(
                        type="agent_event",
                        app_id=_aid,
                        session_id=session_id,
                        user_id=_uid_for_event,
                        op_id=agent_id,
                        op_type=OpType.AGENT,
                        op_state=op_state,
                        op_parent_id=parent_agent if isinstance(parent_agent, str) else None,
                        payload=agent_payload,
                    )))
                    return

                # Memory events (todos, goal, facts)
                frontend_action = _MEMORY_EVENT_MAP.get(event_type)
                if frontend_action is not None:
                    payload: dict = {"action": frontend_action}
                    if event_type in ("todo_added", "todo_updated"):
                        payload["result"] = {
                            "todos": notification.get("todos", []),
                            "todo": notification.get("todo"),
                            "goal": notification.get("goal", ""),
                            "progress": notification.get("progress", {}),
                        }
                    elif event_type == "goal_set":
                        payload["result"] = {"goal": notification.get("goal", "")}
                    elif event_type == "fact_added":
                        payload["result"] = {
                            "id": notification.get("id"),
                            "content": notification.get("content"),
                        }
                    elif event_type == "fact_removed":
                        payload["result"] = {"id": notification.get("id")}
                    loop.create_task(_bus.publish(bus_key, {
                        "type": "memory_update",
                        "data": payload,
                    }))

            cb._on_notification_relay = _relay

            _manager_for_bridge = self
            _aid_for_bridge = _aid
            def _terminal_agent_bridge(
                notification: dict, sid: str,
            ) -> None:
                _user_id = (
                    notification.get("user_id")
                    or getattr(deployed, "user_id", None)
                    or "local"
                )
                try:
                    loop.create_task(
                        _manager_for_bridge.check_notifications(
                            _aid_for_bridge, sid, user_id=_user_id,
                        ),
                        name=f"agent_wakeup:{notification.get('agent_id', '?')}",
                    )
                except Exception as exc:
                    logger.debug(
                        "terminal_agent_bridge schedule failed: %s", exc,
                    )

            cb._on_terminal_agent_event = _terminal_agent_bridge

        # When enabled, watch the bundle's prompts/skills/assets
        # dirs and auto-redeploy on changes. Default off.
        try:
            settings = getattr(self, "_settings", None)
            hot_reload_enabled = bool(
                settings and getattr(settings.app, "hot_reload", False)
            )
        except Exception:
            hot_reload_enabled = False
        if hot_reload_enabled and compiled.source_path:
            try:
                from pathlib import Path
                from digitorn.core.app.hot_reload import BundleHotReloader
                bundle_dir = Path(compiled.source_path).parent
                async def _on_reload():
                    try:
                        await self.redeploy(app_id)
                    except Exception as exc:
                        logger.warning(
                            "hot_reload redeploy failed app=%s: %s",
                            app_id, exc,
                        )
                reloader = BundleHotReloader(
                    app_id=app_id,
                    bundle_dir=bundle_dir,
                    on_change=_on_reload,
                )
                await reloader.start()
                deployed.hot_reloader = reloader
            except Exception as exc:
                logger.debug(
                    "hot_reload start skipped for %s: %s", app_id, exc,
                )

        restored = 0
        if cb is not None and hasattr(cb, "restore_watchers"):
            try:
                restored = await cb.restore_watchers(app_id)
            except Exception as exc:
                logger.warning("watcher_restore_failed app=%s: %s", app_id, exc, exc_info=True)

        if compiled.execution.scheduler and not self._scheduler._running:
            try:
                await self._scheduler.start()
            except Exception as exc:
                logger.warning("scheduler_start_failed: %s", exc, exc_info=True)

        if self._should_sandbox(compiled):
            if self._should_use_pool(compiled):
                pool = await self._deploy_pool(compiled, agent_result)
                if pool is not None:
                    deployed.sandbox_pool = pool
            else:
                worker = await self._deploy_sandboxed(compiled, agent_result)
                if worker is not None:
                    deployed.sandbox_worker = worker

        sandbox_mode = "pool" if deployed.sandbox_pool else ("worker" if deployed.sandbox_worker else "none")
        logger.info(
            "App '%s' deployed: %d agents, %d tools, sandbox=%s",
            app_id,
            len(deployed.contexts),
            deployed.index.total_tools if deployed.index else 0,
            sandbox_mode,
        )

        if compiled.execution.mode == "background":
            _bg_task = asyncio.create_task(
                self._auto_start_background(deployed, compiled)
            )
            self._bg_start_tasks.add(_bg_task)
            _bg_task.add_done_callback(self._bg_start_tasks.discard)

        return deployed

    async def _auto_start_background(self, deployed: Any, compiled: Any) -> None:
        """Auto-start a background mode app after deployment."""
        import copy
        from digitorn.core.runtime.types import apply_workspace_override

        app_id = compiled.app_id
        try:
            from digitorn.core.runtime.modes.background import run_background

            # Create a proper context copy with session and workspace
            ctx = copy.copy(deployed.entry_context)
            ctx.session_id = f"background-{app_id}"
            ctx.app_id = app_id

            yaml_ws = getattr(compiled.execution, "workspace", "")
            ws = yaml_ws or str(Path.cwd())
            apply_workspace_override(ctx, ws, yaml_ws)

            triggers = compiled.execution.triggers or []

            logger.info(
                "background_auto_start app=%s triggers=%d channels_module=%s",
                app_id, len(triggers),
                "yes" if "channels" in deployed.modules else "no",
            )

            channels_mod = deployed.modules.get("channels")
            if channels_mod is not None:
                try:
                    channels_mod._runtime_app = deployed  # type: ignore[attr-defined]
                    channels_mod._hook_runner = deployed.hook_runner  # type: ignore[attr-defined]
                except Exception as exc:
                    logger.debug("_deploy best-effort block failed: %s", exc)

            await run_background(
                ctx,
                triggers=[t for t in triggers],
                max_turns=compiled.execution.max_turns,
                timeout=compiled.execution.timeout,
                app_id=app_id,
                max_concurrent_activations=compiled.execution.max_concurrent_activations,
                runtime_app=deployed,
            )
        except asyncio.CancelledError:
            logger.info("background_app_stopped app=%s", app_id)
        except Exception as exc:
            logger.error("background_auto_start_failed app=%s: %s", app_id, exc, exc_info=True)

    async def _deploy_sandboxed(
        self,
        compiled: CompiledApp,
        bootstrap_result: dict[str, Any],
    ) -> "SandboxWorker | None":
        """Create a sandbox worker for OS-isolated tool execution (standard level)."""
        from digitorn.core.sandbox.worker import SandboxWorker
        from digitorn.core.sandbox.builder import build_sandbox_profile

        app_id = compiled.app_id
        sandboxed_modules = self._get_sandboxed_modules(compiled)
        if not sandboxed_modules:
            return None

        workspace = getattr(compiled.execution, "workspace", "") or ""
        profile = build_sandbox_profile(compiled, workspace_override=workspace or None)

        worker = SandboxWorker(
            app_id=app_id,
            module_ids=sandboxed_modules,
            workspace=workspace,
            allowed_paths=list(profile.writable_paths | profile.readable_paths),
            sandbox_config={
                "allow_exec": profile.allow_exec,
                "allow_fork": profile.allow_fork,
                "allow_network": profile.allow_network,
                "hardening": True,
            },
        )

        try:
            await worker.start()
        except Exception as exc:
            logger.warning("sandbox_worker_failed app=%s: %s", app_id, exc)
            return None

        return worker

    async def _deploy_pool(
        self,
        compiled: CompiledApp,
        bootstrap_result: dict[str, Any],
    ) -> "WorkerPool | None":
        """Create a WorkerPool for per-session OS isolation (strict/maximum level)."""
        from digitorn.core.sandbox.pool import WorkerPool

        app_id = compiled.app_id
        sandboxed_modules = self._get_sandboxed_modules(compiled)
        if not sandboxed_modules:
            return None

        sandbox_cfg = self._get_sandbox_config(compiled)
        level = getattr(sandbox_cfg, "level", "strict") if sandbox_cfg else "strict"

        # Resource-efficient defaults for scale
        pool_size = getattr(sandbox_cfg, "pool_size", 0) if sandbox_cfg else 0
        pool_max = getattr(sandbox_cfg, "pool_max", 4) if sandbox_cfg else 4

        # Namespaces based on level
        namespaces: set[str] = set()
        if sandbox_cfg and hasattr(sandbox_cfg, "namespaces"):
            namespaces = set(sandbox_cfg.namespaces)
        elif level == "strict":
            namespaces = {"user", "pid"}
        elif level == "maximum":
            namespaces = {"user", "pid", "net"}

        # Hardening config
        hardening = {"enabled": True, "drop_caps": True, "mdwe": True, "no_dumpable": True}

        # Audit for maximum level only
        audit = level == "maximum"
        workspace_snapshot = getattr(sandbox_cfg, "workspace_snapshot", False) if sandbox_cfg else False

        pool = WorkerPool(
            compiled=compiled,
            app_id=app_id,
            pool_size=pool_size,
            pool_max=pool_max,
            namespaces=namespaces,
            hardening=hardening,
            audit=audit,
            workspace_snapshot=workspace_snapshot,
        )

        try:
            await pool.start()
        except Exception as exc:
            logger.warning("sandbox_pool_failed app=%s: %s", app_id, exc)
            return None

        logger.info(
            "sandbox_pool_deployed app=%s level=%s pool_size=%d pool_max=%d ns=%s audit=%s",
            app_id, level, pool_size, pool_max, namespaces, audit,
        )
        return pool

    @staticmethod
    def _get_sandboxed_modules(compiled: CompiledApp) -> list[str]:
        sandboxed = []
        for mid in compiled.module_ids:
            if mid in ("filesystem", "shell", "database", "git", "notebook"):
                sandboxed.append(mid)
        return sandboxed

    @staticmethod
    def _refresh_agent_tools(
        compiled: CompiledApp,
        agent_result: dict[str, Any],
        cb: Any,
        new_index: Any,
    ) -> None:
        """Rebuild agent tool lists after the tool index changed."""
        from digitorn.modules.context_builder.builder import build_direct_tools
        from digitorn.modules.context_builder.prompt import build_system_prompt
        from digitorn.core.runtime.bootstrap import (
            _build_meta_tools_schema,
            _build_primitive_tools_schema,
            _choose_tool_injection,
        )

        _tc_block = (
            getattr(getattr(compiled, "ui", None), "chat_tool_calls", None)
            if compiled is not None else None
        )
        _inject_intent = bool(getattr(_tc_block, "inject_intent", False)) if _tc_block else False
        direct_tools = build_direct_tools(new_index, inject_intent=_inject_intent)
        meta_tools = _build_meta_tools_schema(cb)

        contexts: dict[str, AgentContext] = agent_result["contexts"]
        for agent_id, ctx in contexts.items():
            tool_injection = _choose_tool_injection(
                total_tools=new_index.total_tools,
                context_window=ctx.context_config.max_tokens,
                direct_tools=direct_tools,
            )

            if tool_injection == "direct":
                primitive_tools = _build_primitive_tools_schema(
                    cb,
                    watchers_enabled=ctx.watchers_enabled,
                    channels_enabled=bool(compiled.channels),
                )
                agent_tools = direct_tools + primitive_tools
            else:
                agent_tools = meta_tools

            ctx.tools = agent_tools
            ctx.tool_injection = tool_injection

            agent_def = next(
                (a for a in compiled.agents if a.agent_id == agent_id), None,
            )
            if agent_def is not None:
                ctx.system_prompt = build_system_prompt(
                    agent_id=agent_id,
                    role=ctx.role,
                    user_prompt=agent_def.system_prompt,
                    index=new_index,
                    native_tool_use=ctx.native_tool_use,
                    tool_injection=tool_injection,
                    tools=agent_tools,
                    plan_first=ctx.plan_first,
                    setup_summary=ctx.setup_summary,
                    channels_info=ctx.channels_info,
                    default_channel=ctx.default_channel,
                )

            logger.debug(
                "agent_tools_refreshed agent=%s mode=%s tools=%d",
                agent_id, tool_injection, len(agent_tools),
            )

    def _get_deployed(
        self,
        app_id: str,
        user_id: str | None = None,
    ) -> DeployedApp:
        """Get a deployed app or raise - scope-aware."""
        deployed = self.get(app_id, user_id=user_id)
        if deployed is None:
            available = list(self._deployed.keys())
            raise RuntimeError(
                f"App '{app_id}' not deployed (available: {available})"
            )
        return deployed

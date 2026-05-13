"""_DeployMixin - full app deployment lifecycle.

Owns: deploy / undeploy / enable / disable / delete / reload paths,
the resolution helpers (``get`` / ``_get_deployed`` / ``_deployed_key``),
and the sandbox scaffolding (``_deploy_sandboxed`` / ``_deploy_pool``).
"""

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


# ── DB transient-failure retry helper ────────────────────────────────
# Deploys fail in cascade when the Postgres host has a transient DNS
# blip (Wi-Fi reconnect, AWS endpoint flap, DNS cache miss). Each app's
# bundle reload calls ``_secret_store.get_all`` + ``_wipe_user_installs``
# which both hit Postgres; without retry, a one-second hiccup at boot
# loses every builtin app for the entire daemon lifetime.

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
    """Run a coroutine factory with exponential backoff on transient
    DB connectivity failures. ``attempts`` total tries; delays are
    ``base_delay * 2**(i-1)`` with a 10s cap (0.5s, 1s, 2s, 4s by
    default). Non-transient errors propagate immediately.

    Used to wrap the two DB hits the deploy chain makes per app
    (secret read + user-install wipe) so a Wi-Fi blip doesn't take
    down every builtin app at daemon boot.
    """
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
        """Deploy an app from a YAML file.

        Full lifecycle: compile → bootstrap (setup steps) → build agent
        contexts → sync to DB → register in runtime store.

        Args:
            yaml_path: Path to the app YAML file.
            force: Re-deploy even if already deployed.

        Returns:
            DeployedApp ready for execution.

        Raises:
            AppCompilationError: If YAML validation fails.
            RuntimeError: If bootstrap or agent context building fails.
        """
        import yaml as _yaml

        # Sync disk read + YAML parse: off-load to a thread so a deploy
        # call from an HTTP handler never stalls other coroutines on
        # the loop. With multi-app reload at boot the cumulative cost
        # is the difference between a 100 ms boot and a 30 s freeze.
        def _read_and_parse() -> dict[str, Any]:
            return _yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}

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

        # Merge legacy per-app secrets with the new CredentialStore
        # (system_wide + per_app_shared scopes visible at compile time).
        # Per-user scopes are resolved at runtime, not here - the
        # compile has no user context.
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

        # YAML parse + validation + transforms is CPU-bound; off-load.
        compiled = await asyncio.to_thread(
            self._compiler.compile_file,
            yaml_path, secrets=db_secrets or None,
        )
        app_id = compiled.app_id

        # Surface compile warnings to the daemon log. These are
        # non-fatal smells the compiler caught (triggers in
        # non-background mode silently ignored, compact_context hook in
        # one_shot, ...). Visible in journalctl + the API summary.
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

            # BUG-081 (force=True redeploy): build the NEW DeployedApp
            # BEFORE tearing down the old one. If build fails, rollback
            # to the previous deploy atomically so a user with
            # ``force: true`` can't nuke a system-scope builtin
            # (``digitorn-chat``, …) by POSTing a YAML that fails to
            # compile. Without this, every user of that builtin got
            # 404 on their next message.
            self._deployed.pop(deployed_key, None)
            try:
                new_deployed = await self._build_and_deploy(
                    compiled, scope=scope, owner_user_id=owner_user_id,
                )
            except Exception:
                # Rollback: the build failed, put the old app back
                # verbatim. Users of the previous deploy see no
                # interruption.
                self._deployed[deployed_key] = previous
                logger.warning(
                    "Deploy of '%s' FAILED - rolled back to previous "
                    "deploy to keep existing users online.", app_id,
                )
                raise

            # Build succeeded - retire the previous deploy cleanly now
            # that the replacement is in place. Module-level shutdown
            # only; the heavier session/circuit-breaker teardown stays
            # on the ``undeploy()`` path because it's destructive to
            # conversation state - a silent redeploy keeps users
            # online rather than nuking their sessions.
            #
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
        """Run a deployed app in one-shot mode.

        Args:
            app_id: The deployed app's ID.
            user_input: User input text.
            user_id: Caller - used to resolve user-scoped deploys.
            on_tool_call: Optional callback for tool call display.

        Returns:
            TurnResult with the agent's response.
        """
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
        """Get a RuntimeApp executor for conversation mode.

        Returns a RuntimeApp that the caller can use to run conversation/background.
        The app stays deployed after the conversation ends.
        """
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
        """Build the ``_deployed`` dict key for a (app_id, scope,
        owner) tuple.

        System: ``system::<app_id>``
        User:   ``user:<uid>:<app_id>``

        This lets the same app_id be deployed in two scopes at
        once (admin system install + user override) without
        collision in the shared map.
        """
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
        """Get a deployed app by ID, resolved for a specific caller.

        Resolution order:
          1. User-scoped deploy owned by ``user_id`` (when provided)
          2. System-scoped deploy
          3. Legacy: bare ``app_id`` key (backwards compat for
             tests and old code paths that haven't been updated)

        Returns None if nothing matches.
        """
        if user_id:
            user_key = self._deployed_key(app_id, "user", user_id)
            hit = self._deployed.get(user_key)
            if hit is not None:
                return hit
        system_key = self._deployed_key(app_id, "system")
        hit = self._deployed.get(system_key)
        if hit is not None:
            return hit
        # Legacy bare key - kept for backwards compat with old
        # call sites that pre-date the scoping refactor.
        legacy = self._deployed.get(app_id)
        if legacy is not None:
            return legacy
        # Last resort: scan any user-scoped deploy of this app. Needed
        # for admin-style tools (diagnostics, /api/apps listing from a
        # session-less caller) that previously returned "not deployed"
        # for every user-scoped app because no user_id was passed in.
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
        """List deployed apps visible to a caller.

        Without ``user_id``, returns every deploy (admin view).
        With ``user_id``, returns:
          - every system-scoped deploy
          - every user-scoped deploy belonging to the caller
        Any system deploy shadowed by a user deploy of the same
        app_id is hidden (user version wins).

        Disabled apps are invisible here - they're not in ``_deployed``.
        Use ``list_disabled_apps()`` (admin-only at the API layer) to
        surface them.
        """
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
        """Return a minimal summary of every disabled app.

        Scoping: when ``user_id`` is provided, returns only:
          - the user's own disabled installs (scope='user', owner=user_id)
          - every disabled system install (scope='system')
        When ``user_id`` is None (admin view), returns every disabled
        install across all scopes.

        Disabled apps are not in ``_deployed``; this reads from DB.
        """
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

        return [
            {
                "app_id": a.app_id,
                "scope": a.scope,
                "owner_user_id": a.owner_user_id,
                "name": a.name,
                "version": a.version,
                "disabled": True,
                "disabled_at": a.disabled_at.isoformat() if a.disabled_at else None,
                "disabled_reason": a.disabled_reason or "",
                "has_bundle": a.current_bundle_id is not None,
            }
            for a in rows
        ]

    def is_deployed(
        self, app_id: str, *, user_id: str | None = None,
    ) -> bool:
        """Check if an app is deployed (and visible to the caller
        when ``user_id`` is provided)."""
        return self.get(app_id, user_id=user_id) is not None

    async def undeploy(
        self, app_id: str, *, user_id: str | None = None,
    ) -> bool:
        """Undeploy an app - graceful shutdown of all its modules.

        Scope-aware: when ``user_id`` is passed, targets the user-
        scoped deploy belonging to that user. Without it, targets
        the system-scoped deploy. Falls back to legacy bare key
        lookup for backwards compat.

        Returns True if the app was deployed and is now removed.
        Built-in apps cannot be undeployed.
        """
        # Resolve which key to pop
        if user_id:
            key = self._deployed_key(app_id, "user", user_id)
        else:
            key = self._deployed_key(app_id, "system")
        deployed = self._deployed.get(key)
        if deployed is None:
            # Legacy bare key fallback
            deployed = self._deployed.get(app_id)
            if deployed is None:
                return False
            key = app_id
        if getattr(deployed, "builtin", False):
            raise RuntimeError(f"Cannot undeploy built-in app '{app_id}'")
        self._deployed.pop(key, None)

        # Stop the hot reloader if present - must run before the
        # other shutdowns so it doesn't try to redeploy mid-undeploy.
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
        # Cancel running scheduled tasks BEFORE unregistering executor
        # so they don't fire one last time and try to call the
        # now-missing executor/wake handler. Without this, every undeployed
        # app leaks its scheduler asyncio tasks - they keep firing forever
        # and respawn themselves at next_run_at, generating log spam +
        # CPU work for an app that no longer exists.
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
        """Permanently remove a scoped app install - memory, bundles, DB rows, secrets.

        **Multi-tenant scoping** (identifies which install to remove):

        - Pass ``user_id="alice"`` to remove Alice's private install.
          Bob's install of the same ``app_id`` is untouched; so is any
          system install.
        - Pass ``scope="system"`` (admin path) to force removal of the
          system install even when a user_id is available.
        - Pass nothing (default): the caller is acting on a system
          install - matches legacy behaviour.

        Pipeline (hard delete):

        1. ``undeploy(app_id, user_id=...)`` - stops the scoped in-memory
           instance, shuts down sandbox, cancels approvals, drains
           sessions.
        2. Wipe the app's scoped directory on disk (scope-aware: system
           stays at ``~/.digitorn/apps/{app_id}/``, user installs use
           ``~/.digitorn/apps/_@{uid}__{app_id}/`` - see
           ``_scoped_slug``). Other scopes of the same app_id are
           **NOT** touched.
        3. Delete the single matching ``Application`` row. SQLAlchemy's
           cascade removes its ``AppProfile``, ``AppModuleGrant``,
           ``AppModuleConfig``, ``AppBundle`` and (when
           ``delete_history=True``) sessions/messages/activations.
        4. Purge the secret store for this scope.

        Built-in apps raise ``RuntimeError``.

        Returns::

            {
                "app_id": "...",
                "scope": "system" | "user",
                "owner_user_id": "" | "<uid>",
                "deployed": bool,
                "bundles_deleted": int,
                "disk_removed": bool,
                "secrets_deleted": int,
                "db_removed": bool,
                "history_preserved": bool,
            }
        """
        from digitorn.core.app.bundle_store import BundleStoreError

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
            "bundles_deleted": 0,
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

        # Step 2 - delete DB rows.
        #
        # ORDER MATTERS: DB FIRST, disk wipe AFTER. If we wiped disk
        # first and the DB delete then crashed, the daemon would
        # restart with rows pointing at non-existent bundles and emit
        # "Bundle … missing on disk - falling back to legacy yaml_content"
        # warnings forever. By deleting the DB rows first, a disk-wipe
        # failure leaves only orphan files (no DB row references them)
        # which is harmless - the next deploy or the periodic sync
        # cleans them up, and no startup warning fires.
        #
        # Use explicit SQL via `get_session_factory` so we blow up
        # loudly (instead of silently no-op) when the DB isn't initialised.
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
                        # Break the FK from applications → app_bundles for
                        # THIS scope only. Other scopes stay intact.
                        await session.execute(
                            _sql_text(
                                f"UPDATE applications SET current_bundle_id = NULL "
                                f"WHERE {scope_filter}"
                            ),
                            params,
                        )
                        if delete_history:
                            # Hard delete for THIS scope. ORM cascade
                            # covers AppProfile, AppModuleConfig and
                            # UserSession (those still have FKs). We
                            # explicitly delete app_bundles rows because
                            # we dropped that FK in the scoping refactor
                            # (composite keys can't be single-column FK
                            # in SQLite).
                            await session.execute(
                                _sql_text(
                                    "DELETE FROM app_bundles "
                                    "WHERE app_id = :a "
                                    "  AND scope = :s "
                                    "  AND owner_user_id = :o"
                                ),
                                params,
                            )
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
                            # Delete bundle rows for THIS scoped app row.
                            # app_bundles also carries (scope, owner_user_id)
                            # since the refactor so the filter is direct.
                            await session.execute(
                                _sql_text(
                                    "DELETE FROM app_bundles "
                                    "WHERE app_id = :a "
                                    "  AND scope = :s "
                                    "  AND owner_user_id = :o"
                                ),
                                params,
                            )
                            result["db_removed"] = False
            except Exception as exc:
                logger.error(
                    "DB cleanup failed for '%s' scope=%s owner=%s: %s",
                    app_id, resolved_scope, resolved_owner, exc, exc_info=True,
                )
                raise

        # Step 3 - delete bundles from disk for THIS scope.
        #
        # Runs AFTER the DB delete - see the ORDER MATTERS comment in
        # Step 2. The scoped_slug isolates user installs so Bob's copy
        # survives when Alice runs delete. Failures here only leave
        # orphan files (no DB row points at them) - safe.
        try:
            # Off-loop: ``delete_app`` walks every bundle dir and
            # ``shutil.rmtree``s it. Bundles can carry node_modules /
            # build artefacts (50-200 MB) -- doing this on the main
            # loop blocked the watchdog for 2-10s every undeploy.
            bundle_count = await asyncio.to_thread(
                self._bundle_store.delete_app, scoped_slug,
            )
            result["bundles_deleted"] = bundle_count
        except BundleStoreError as exc:
            logger.warning("bundle cleanup failed for '%s': %s", scoped_slug, exc)
        except Exception as exc:
            logger.warning(
                "bundle cleanup raised unexpected error for '%s': %s",
                scoped_slug, exc, exc_info=True,
            )

        # Wipe any leftover files inside the scoped app dir.
        # Off-loop: ``rmtree`` of an app dir with a populated workspace
        # (node_modules, build artefacts) routinely takes seconds.
        import asyncio as _asyncio
        import shutil
        app_dir = Path.home() / ".digitorn" / "apps" / scoped_slug
        try:
            if app_dir.exists():
                await _asyncio.to_thread(shutil.rmtree, app_dir, False)
                result["disk_removed"] = True
            else:
                # Previously reported True here, which caused the API to
                # tell callers "disk_removed: true" even when there was
                # nothing to remove (BUG-048 - user deletes a built-in
                # system app they never installed, gets a success dict
                # detailing fictional cleanup).
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
            "app_deleted app=%s scope=%s owner=%r deployed=%s bundles=%d "
            "disk=%s secrets=%d db=%s history=%s",
            app_id,
            resolved_scope,
            resolved_owner,
            result["deployed"],
            result["bundles_deleted"],
            result["disk_removed"],
            result["secrets_deleted"],
            result["db_removed"],
            "preserved" if result["history_preserved"] else "purged",
        )
        # Truth-check: if absolutely nothing changed on disk, in DB, or
        # in memory, this was a no-op (user asked to delete something
        # that doesn't belong to them / doesn't exist at this scope).
        # Previously the response still said `deleted: true`; callers
        # believed their data was wiped when it wasn't.
        nothing_happened = (
            not result["deployed"]
            and result["bundles_deleted"] == 0
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
        """Disable a scoped app install: undeploy + hide from non-admin list/get.

        Differs from ``delete_app`` in that nothing is removed from disk
        or DB - disabling is fully reversible via ``enable_app``. Only
        the install matching ``(app_id, scope, owner_user_id)`` is
        disabled; other scopes of the same app_id stay live.

        Built-in apps cannot be disabled.
        """
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
        """Re-enable a disabled scoped install and redeploy it.

        Admin-only at the API layer. The single install matching
        ``(app_id, scope, owner_user_id)`` is flipped back to
        ``disabled=False`` and redeployed from its stored bundle.
        Fails if the bundle was wiped (e.g. previous
        ``delete_history=False`` call).
        """
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import AppBundle, Application
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
                if app_row.current_bundle_id is None:
                    raise RuntimeError(
                        f"App '{app_id}' cannot be re-enabled: no bundle "
                        f"(deleted with delete_history=False)."
                    )
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

        # Redeploy from the saved bundle (scope-aware bundle path).
        redeployed = False
        try:
            async with sf() as session:
                row = await session.execute(
                    select(Application).where(
                        Application.app_id == app_id,
                        Application.scope == resolved_scope,
                        Application.owner_user_id == resolved_owner,
                    )
                )
                app_row = row.scalar_one_or_none()
                if app_row and app_row.current_bundle_id:
                    br = await session.execute(
                        select(AppBundle).where(AppBundle.id == app_row.current_bundle_id)
                    )
                    bundle_row = br.scalar_one_or_none()
                    if bundle_row is not None:
                        scoped = _scoped_slug(app_id, resolved_scope, resolved_owner)
                        descriptor = self._bundle_store.get_by_path(
                            scoped, bundle_row.bundle_path,
                        )
                        if descriptor is not None:
                            await self._deploy_from_bundle(
                                descriptor,
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
        """Remove every USER-scope install of ``app_id`` - SYSTEM wins.

        Called at the top of ``_build_and_deploy`` whenever a deploy
        lands at ``scope="system"``. Every existing
        ``(app_id, scope="user", owner_user_id=*)`` install is hard-
        deleted: in-memory, on-disk bundle, DB row, install dir. Sessions
        are kept (``delete_history=False``) so users don't lose their
        chat history when an admin promotes a user-scope app to system.

        Idempotent: when no user installs exist, this is a single SELECT
        and returns immediately. No-op when the daemon's DB isn't wired
        (test paths).
        """
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
            # Retry on transient DB hiccups (Wi-Fi blip, DNS cache miss,
            # Neon endpoint flap). Without retry a one-second outage at
            # boot cascades into every builtin failing to reload.
            user_owners = await _retry_db_call(
                _do_query, label=f"wipe_user_installs:{app_id}",
            )
        except Exception as exc:
            logger.warning(
                "wipe_user_installs DB query failed after retries app=%s: %s",
                app_id, exc,
            )
            return

        # Also catch in-memory user deploys that may not have a DB row
        # (transient state during fresh installs). Keys are strings:
        # ``user:<uid>:<app_id>`` for user scope, ``system::<app_id>``
        # for system. We only care about the user variant here.
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
        """Hot-reload a single deployed app from its current bundle.

        Use this when a persistent resource the app depends on has
        changed and the in-memory instance is now stale - typically
        after a secret / API key rotation, a module config tweak, or an
        external dependency swap.

        Pipeline:

        1. Load the ``Application`` row + its ``current_bundle`` from DB.
        2. Stop the currently-running in-memory instance (``undeploy``).
        3. Re-read the bundle from disk via ``BundleStore``.
        4. Recompile using the **fresh** secrets from ``SecretStore`` -
           so a PUT /secrets/{key} made just before this call is picked
           up automatically.
        5. Re-bootstrap the app and put it back in ``_deployed``.

        The DB rows are NOT modified - same ``app_id``, same bundle
        hash, same profile / grants / configs. Only the in-memory state
        is rebuilt. Sessions tied to the app are dropped (they would be
        inconsistent with the new module state anyway).

        Returns a status dict: ``{app_id, reloaded, bundle_hash,
        secrets_applied}``.

        Raises:
            KeyError: if the app is not in the DB.
            FileNotFoundError: if the bundle is missing from disk.
            RuntimeError: if the app is built-in (built-ins are reloaded
                via ``_deploy_builtin_apps`` at daemon startup).
        """
        from sqlalchemy import select as _select
        from sqlalchemy.orm import selectinload as _selectinload

        from digitorn.core.database import get_session_factory
        from digitorn.core.models import AppBundle as _AppBundle
        from digitorn.core.models import Application as _Application

        # Built-in apps own their lifecycle via _deploy_builtin_apps.
        existing = self._deployed.get(app_id)
        if existing is not None and getattr(existing, "builtin", False):
            raise RuntimeError(
                f"Cannot hot-reload built-in app '{app_id}' - "
                f"restart the daemon to pick up changes.",
            )

        # Fetch the app + its current bundle from DB. Apps may exist at
        # both system and per-user scopes (same ``app_id`` deployed by
        # multiple users). Without ordering, ``scalar_one_or_none`` would
        # raise ``MultipleResultsFound`` and the reload returned 500.
        # Prefer the system-scope row (the deploy that drives the in-
        # memory ``self._deployed[app_id]`` instance reload_app is
        # rebuilding) and fall back to the first user-scope row when
        # only user installs exist.
        _sf = get_session_factory()
        async with _sf() as session:
            result = await session.execute(
                _select(_Application)
                .options(_selectinload(_Application.current_bundle))
                .where(_Application.app_id == app_id)
                .order_by(_Application.scope.asc())
                .limit(1)
            )
            app_row = result.scalars().first()

        if app_row is None:
            raise KeyError(f"App '{app_id}' not found in database.")

        bundle_row: _AppBundle | None = app_row.current_bundle
        if bundle_row is None:
            # Legacy app without a bundle - fall back to yaml_content
            # reload. Rare: only happens on pre-bundle deploys that
            # never got re-deployed after the bundle refactor.
            if not app_row.yaml_content:
                raise FileNotFoundError(
                    f"App '{app_id}' has no bundle AND no yaml_content. "
                    f"Deploy it again from the source YAML.",
                )
            await self._deploy_from_content(
                app_row.yaml_content,
                source=app_row.yaml_path or app_id,
            )
            return {
                "app_id": app_id,
                "reloaded": True,
                "bundle_hash": None,
                "secrets_applied": 0,
                "source": "legacy_yaml_content",
            }

        descriptor = self._bundle_store.get_by_path(
            app_id, bundle_row.bundle_path,
        )
        if descriptor is None:
            raise FileNotFoundError(
                f"Bundle for '{app_id}' is missing on disk at "
                f"{bundle_row.bundle_path}. Re-deploy the app.",
            )

        # Count secrets for the return payload (so the caller knows
        # how many keys are currently active).
        try:
            current_secrets = await self._secret_store.get_all(app_id)
        except Exception:
            current_secrets = {}

        # _deploy_from_bundle undeploys the old instance, recompiles
        # with fresh secrets from SecretStore, and re-bootstraps.
        await self._deploy_from_bundle(descriptor)

        logger.info(
            "app_reloaded app=%s bundle=%s secrets=%d",
            app_id, descriptor.short_hash, len(current_secrets),
        )

        return {
            "app_id": app_id,
            "reloaded": True,
            "bundle_hash": descriptor.bundle_hash,
            "secrets_applied": len(current_secrets),
            "source": "bundle",
        }

    async def reload_from_db(self, *, parallelism: int = 16) -> list[str]:
        """Reload all apps from the database at daemon startup.

        Priority order for recompilation:
        1. AppBundle on disk (via ``current_bundle_id``) - the primary
           path since the bundle contains the YAML plus every referenced
           asset (skills, prompts, …). The source filesystem is never
           touched.
        2. Legacy fallback: ``yaml_content`` stored directly on the
           Application row (pre-bundle deploys). This path will be
           removed once all existing installs have been migrated.

        Apps are reloaded **concurrently** with a bounded semaphore
        (default width 16, was 4 previously). Width 16 is the sweet
        spot for a typical workstation: enough fan-out that 50+ apps
        warmup in ~15-30s instead of 2 min, while still serialising
        the most contended shared resources (Postgres connection
        pool default = 20, fastembed model load, MCP stdio
        children). Bump higher on beefy hosts with monitoring;
        lower if you see ``QueuePool overflow`` warnings.

        Returns list of app_ids that were successfully reloaded.
        """
        import asyncio as _asyncio

        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from digitorn.core.database import _session_factory
        from digitorn.core.models import Application

        if _session_factory is None:
            logger.warning("Cannot reload apps: database not initialized")
            return []

        async with _session_factory() as session:
            result = await session.execute(
                select(Application).options(selectinload(Application.current_bundle))
            )
            apps = list(result.scalars().all())

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
        return reloaded

    async def _reload_one_app(self, app_row: Any) -> str | None:
        """Reload a single app - the body of the loop extracted so
        ``reload_from_db`` can run them in parallel. Returns the
        ``app_id`` on success, ``None`` on skip / purge, and raises on
        hard failure (the caller logs with ``exc_info``).
        """
        from sqlalchemy import delete as _delete
        from sqlalchemy import text as _sql_text
        from sqlalchemy import update as _update

        from digitorn.core.database import get_session_factory
        from digitorn.core.models import AppBundle, Application

        # Keep the original variable names so the inlined body (copied
        # verbatim from the old ``for`` loop) keeps working unchanged.
        app_id = app_row.app_id
        row_scope = getattr(app_row, "scope", "system") or "system"
        row_owner = getattr(app_row, "owner_user_id", "") or ""

        # Skip disabled apps - they stay registered in DB but are not
        # deployed to memory. Admins re-enable via enable_app which
        # re-reads the bundle and calls _deploy_from_bundle directly.
        if getattr(app_row, "disabled", False):
            logger.info(
                "reload_skip_disabled app=%s scope=%s owner=%r",
                app_id, row_scope, row_owner,
            )
            return None

        # Source-of-truth shortcut: when the package install_dir exists
        # AND holds an ``app.yaml`` on disk, prefer compiling directly
        # from there. ``compile_file`` runs the source-tree
        # ``apply_includes`` which picks up every convention fragment
        # (templates.yaml, agents/, hooks/, ...) and records them in
        # ``collected_assets`` — so the bundle written by the
        # subsequent sync is ALWAYS complete.
        #
        # Without this shortcut we'd go through Path A (bundle reload),
        # which is fragile: if the bundle dir was wiped by the
        # single-bundle policy, OR if it was created by a buggy earlier
        # build with ``assets:[]``, the asset_loader returns None for
        # convention files and ``compiled.collected_assets`` ends up
        # empty. The subsequent sync then re-writes an empty bundle,
        # which sticks at restart-loop: templates / agents / hooks
        # silently vanish on every subsequent restart.
        try:
            _install_dir = await self._resolve_install_dir(
                app_id, user_id=row_owner or None,
            )
        except Exception:
            _install_dir = None
        if _install_dir is not None:
            _candidate = _install_dir / "app.yaml"
            if _candidate.is_file():
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
                        _candidate, secrets=db_secrets or None,
                    )
                    logger.info(
                        "Reload from install_dir for '%s' "
                        "(scope=%s owner=%r dir=%s) — bypassing bundle",
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
                        "Reload from install_dir FAILED for '%s' "
                        "(scope=%s): %s — falling back to bundle/legacy",
                        app_id, row_scope, exc, exc_info=True,
                    )

        # Path A - bundle on disk (legacy fallback when install_dir is
        # missing, e.g. an app installed via API content-only that was
        # never written to disk)
        if app_row.current_bundle is not None:
            bundle_row: AppBundle = app_row.current_bundle
            scoped = _scoped_slug(app_id, row_scope, row_owner)
            descriptor = self._bundle_store.get_by_path(
                scoped, bundle_row.bundle_path,
            )
            if descriptor is None:
                # Self-heal: the DB references a bundle that no longer
                # exists on disk (typical aftermath of a botched delete
                # that wiped the disk dir but left the DB row, or a
                # manual ``rm -rf ~/.digitorn/apps/<id>``). NULL the
                # FK + delete the orphan AppBundle row so this warning
                # stops firing on every subsequent reload. The app
                # itself stays loadable via the legacy yaml_content
                # fallback below; the next successful deploy will
                # mint a fresh bundle and re-attach it.
                logger.warning(
                    "Bundle for '%s' (scope=%s) missing on disk at %s - "
                    "auto-clearing the FK and falling back to legacy "
                    "yaml_content. Will be rebuilt on next deploy.",
                    app_id, row_scope, bundle_row.bundle_path,
                )
                try:
                    _orphan_bundle_id = bundle_row.id
                    _sf = get_session_factory()
                    async with _sf() as _s:
                        async with _s.begin():
                            await _s.execute(
                                _sql_text(
                                    "UPDATE applications "
                                    "SET current_bundle_id = NULL "
                                    "WHERE app_id = :a "
                                    "  AND scope = :s "
                                    "  AND owner_user_id = :o"
                                ),
                                {
                                    "a": app_id,
                                    "s": row_scope,
                                    "o": row_owner or "",
                                },
                            )
                            await _s.execute(
                                _sql_text(
                                    "DELETE FROM app_bundles WHERE id = :i"
                                ),
                                {"i": _orphan_bundle_id},
                            )
                except Exception as _heal_exc:
                    logger.debug(
                        "self_heal_orphan_bundle_failed app=%s: %s",
                        app_id, _heal_exc,
                    )
            else:
                # Guard against corrupt bundles (earlier versions
                # of the syncer could write an empty app.yaml
                # when compiling from a legacy yaml_content).
                # If the YAML looks empty or unparseable, drop
                # the bundle and fall through to the legacy path
                # so the next sync rebuilds it correctly.
                try:
                    _yaml_preview = await asyncio.to_thread(
                        self._bundle_store.load_yaml, descriptor,
                    )
                except Exception as exc:
                    logger.error(
                        "Bundle YAML unreadable for '%s' at %s: %s",
                        app_id, bundle_row.bundle_path, exc,
                    )
                    _yaml_preview = ""

                if _yaml_preview.strip():
                    await self._deploy_from_bundle(
                        descriptor,
                        scope=row_scope,
                        owner_user_id=row_owner or None,
                    )
                    return app_id

                logger.warning(
                    "Bundle for '%s' has an empty YAML - likely "
                    "created by a buggy legacy reload. Deleting "
                    "it and falling back to yaml_content so the "
                    "next deploy rebuilds the bundle properly.",
                    app_id,
                )
                try:
                    # Off-loop: rmtree on a bundle dir blocks the loop.
                    await asyncio.to_thread(
                        self._bundle_store.delete_bundle,
                        app_id, descriptor.bundle_hash,
                    )
                except Exception as exc:
                    logger.debug(
                        "failed to delete corrupt bundle %s: %s",
                        descriptor.bundle_path, exc,
                    )
                # Clear the FK so the next sync re-creates a
                # fresh bundle instead of trying to reuse the
                # broken row.
                try:
                    _sf = get_session_factory()
                    async with _sf() as _s:
                        async with _s.begin():
                            await _s.execute(
                                _update(Application)
                                .where(Application.app_id == app_id)
                                .values(current_bundle_id=None)
                            )
                            await _s.execute(
                                AppBundle.__table__.delete().where(
                                    AppBundle.id == bundle_row.id,
                                )
                            )
                except Exception as exc:
                    logger.debug(
                        "failed to clear current_bundle_id for %s: %s",
                        app_id, exc,
                    )

        # Path B - legacy yaml_content (pre-bundle deploys or
        # recovered from a broken bundle above)
        if app_row.yaml_content:
            logger.info(
                "Reloading legacy app '%s' from yaml_content - "
                "bundle will be created on next deploy",
                app_id,
            )
            await self._deploy_from_content(
                app_row.yaml_content,
                source=app_row.yaml_path or app_id,
            )
            return app_id

        # Path C - orphaned row: no bundle AND no yaml_content.
        # Nothing we can reconstruct from. These rows are leftovers
        # from a pre-refactor deploy where the old syncer failed to
        # persist yaml_content (the bug my refactor inherited and
        # then propagated into an empty bundle). They cannot be
        # reloaded and keeping them around just causes the daemon
        # to log errors at every boot. Purge them aggressively.
        logger.warning(
            "Purging orphaned app '%s' - no bundle AND no "
            "yaml_content on disk. Row is unrecoverable.",
            app_id,
        )
        try:
            _sf = get_session_factory()
            async with _sf() as _cleanup_session:
                async with _cleanup_session.begin():
                    # Break any remaining FK loop before delete
                    await _cleanup_session.execute(
                        _update(Application)
                        .where(Application.app_id == app_id)
                        .values(current_bundle_id=None)
                    )
                    await _cleanup_session.execute(
                        _delete(AppBundle).where(
                            AppBundle.app_id == app_id,
                        )
                    )
                    await _cleanup_session.execute(
                        _delete(Application).where(
                            Application.app_id == app_id,
                        )
                    )
            # Also remove any empty bundle directory left on disk.
            try:
                # Off-loop: rmtree of the app's bundles can be heavy.
                await asyncio.to_thread(
                    self._bundle_store.delete_app, app_id,
                )
            except Exception:
                pass
            logger.info("orphan_purged app=%s", app_id)
        except Exception as exc:
            logger.error(
                "failed to purge orphan app '%s': %s",
                app_id, exc, exc_info=True,
            )
        return None

    async def _deploy_from_bundle(
        self, descriptor: Any,
        *,
        scope: str = "system",
        owner_user_id: str | None = None,
    ) -> DeployedApp:
        """Recompile and deploy an app directly from its on-disk bundle.

        The compiler reads YAML + assets through the bundle store's
        asset_loader, so the original source filesystem is never
        accessed. This is the standard path used at daemon startup.

        ``scope`` / ``owner_user_id`` propagate to ``_build_and_deploy``
        so per-user and system installs of the same app_id coexist in
        ``self._deployed`` without overwriting each other.
        """
        # Sync disk read -- off-load so the loop never stalls during
        # multi-app reload at boot.
        yaml_content = await asyncio.to_thread(
            self._bundle_store.load_yaml, descriptor,
        )
        peek_app_id = descriptor.app_id
        db_secrets: dict[str, str] = {}
        try:
            # Retry with exponential backoff on transient DB
            # connectivity failures (DNS blip, conn reset). Without it
            # a one-second hiccup cascades across every builtin app
            # the daemon tries to reload at boot.
            db_secrets = await _retry_db_call(
                lambda: self._secret_store.get_all(peek_app_id),
                label=f"secrets_get_all:{peek_app_id}",
            )
        except Exception as exc:
            logger.warning(
                "Secret store read failed for '%s': %s",
                peek_app_id, exc, exc_info=True,
            )

        # YAML parse + Pydantic validation + transformations is CPU-bound
        # and can take 10-100 ms per app. Off-load to a thread so a hot
        # boot reloading 50 apps doesn't freeze HTTP for 5 s.
        compiled = await asyncio.to_thread(
            self._compiler.compile_string,
            yaml_content,
            source=f"bundle://{descriptor.app_id}/{descriptor.short_hash}",
            secrets=db_secrets or None,
            asset_loader=self._bundle_store.asset_loader(descriptor),
        )
        app_id = compiled.app_id

        # compile_string cannot set ``source_path`` (no real filesystem
        # path went in), but downstream code (web/dist static-serve,
        # workspace sync, ...) needs the bundle's on-disk install dir.
        # Look it up from the package registry and stamp it onto the
        # compiled app.
        install_dir = await self._resolve_install_dir(app_id)
        if install_dir is not None:
            compiled.source_path = install_dir / "app.yaml"

        # Only undeploy the SAME scope - other scopes of the same app_id
        # stay live.
        existing_key = self._deployed_key(app_id, scope, owner_user_id)
        if existing_key in self._deployed:
            await self.undeploy(app_id, user_id=owner_user_id)

        logger.info(
            "Deploying app '%s' scope=%s from bundle %s",
            app_id, scope, descriptor.short_hash,
        )
        return await self._build_and_deploy(
            compiled,
            scope=scope,
            owner_user_id=owner_user_id,
        )

    async def _resolve_install_dir(
        self,
        app_id: str,
        *,
        user_id: str | None = None,
    ) -> "Path | None":
        """Return the on-disk install dir for ``app_id``, or None.

        Thin wrapper around the canonical resolver in
        ``digitorn.core.packages.resolver`` - delegates so every code
        path (preview warmup, static dist serving, asset loading)
        sees the SAME resolution chain:

          1. Registry USER scope (if ``user_id``) - per-user override
          2. Registry SYSTEM scope - canonical shared install
          3. Disk ``~/.digitorn/users/{user_id}/packages/{app_id}/``
          4. Disk ``~/.digitorn/packages/{app_id}/``
          5. Source-tree ``packages/digitorn/builtins/{app_id}/``

        Each step requires the candidate to contain an ``app.yaml`` -
        otherwise it's not a valid install and we move to the next.
        """
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
        """Deploy an app from stored YAML content (legacy - no bundle).

        Same lifecycle as deploy() but compiles from a string. Used only
        for legacy pre-bundle deploys during reload - new deploys always
        go through ``_deploy_from_bundle``.
        """
        import yaml as _yaml

        raw = _yaml.safe_load(yaml_content)
        peek_app_id = (raw.get("app") or {}).get("app_id", "")
        db_secrets: dict[str, str] = {}
        if peek_app_id:
            try:
                # Retry on transient DB hiccups -- see comment on the
                # other ``_secret_store.get_all`` call site above.
                db_secrets = await _retry_db_call(
                    lambda: self._secret_store.get_all(peek_app_id),
                    label=f"secrets_get_all:{peek_app_id}",
                )
            except Exception as exc:
                logger.warning("Secret store read failed for '%s': %s", peek_app_id, exc, exc_info=True)
        # Prefer compile_file from the package's install_dir whenever it
        # exists on disk. compile_string alone has no source_dir and no
        # asset_loader, so ``apply_includes`` is a no-op — meaning the
        # convention auto-loaders (templates.yaml, agents/, hooks/, ...)
        # silently never run. That used to leave ``compiled.templates``
        # empty on legacy reloads, the syncer would then freeze an
        # empty-asset bundle, the next reload would deem it corrupt and
        # fall back here again — empty-bundle loop.
        #
        # Routing through compile_file when install_dir is resolvable
        # guarantees conventions are picked up, the bundle written by
        # the subsequent sync contains every fragment, and the next
        # reload uses the proper bundle path. compile_string remains
        # the last-resort fallback for truly orphaned rows where the
        # install_dir is gone.
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
                        # Don't bail — fall back to the string path so a
                        # transient compile failure on disk doesn't strand
                        # the app.
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
        """Check if this app should run in a sandboxed worker."""
        if not self._sandbox_enabled or compiled.security_profile is None:
            return False
        return True

    def _should_use_pool(self, compiled: CompiledApp) -> bool:
        """Check if this app needs a WorkerPool (per-session sandbox).

        Pool mode is required when:
        - workspace_mode=required (different workspace per session, Landlock is irreversible)
        - sandbox.level is strict or maximum
        - pool_size is explicitly configured
        """
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
        """Get the sandbox config from execution block, or None."""
        return getattr(compiled.execution, "sandbox", None)

    async def _build_and_deploy(
        self,
        compiled: CompiledApp,
        *,
        scope: str = "system",
        owner_user_id: str | None = None,
    ) -> DeployedApp:
        """Single bootstrap path for all deploy methods.

        Creates per-app module instances, builds agent contexts,
        syncs to DB, and registers the deployed app.

        When sandbox mode is enabled and the app has a security profile,
        the app is forked into an isolated worker subprocess with
        OS-level enforcement (Landlock/seccomp/Seatbelt/Job Objects).
        """
        app_id = compiled.app_id

        # SYSTEM scope wins over USER scope: an install at scope="system"
        # wipes every existing (app_id, scope="user", *) install before
        # the new one lands. Idempotent / no-op when no user installs
        # exist. Runs FIRST so the rest of the deploy never races against
        # a half-disabled user instance.
        if scope == "system":
            await self._wipe_user_installs(app_id)

        from digitorn.core.runtime.bootstrap import bootstrap as build_agent_contexts

        # Resolve `credential:` refs declared in the YAML at deploy-visible
        # scopes (system_wide, per_app_shared) and inject the decrypted
        # fields into the live module/brain configs BEFORE bootstrap reads
        # them. Per-user scopes are skipped here and applied at session
        # start by `session_resolver`. No-op when the credential store
        # isn't wired (dev paths).
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
            # Required-slot misses raise CredentialInjectError - that's
            # the only path that propagates. Any other failure (vault
            # outage, audit-log error) is logged here and the deploy
            # continues with whatever values the YAML carried inline,
            # so a partial credential outage never strands the daemon.
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
            except Exception:
                pass
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

            # Reuse the manager's bundle store so every deploy writes
            # to the same on-disk root.
            syncer = AppSyncer(bundle_store=self._bundle_store)
            # Pass the scope/owner so the row is written under the correct
            # (app_id, scope, owner_user_id) composite key. This is what
            # lets two users install the same app_id in parallel.
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
                # Wire the manager itself onto the context_builder so the
                # ``call_app`` meta-tool can dispatch in-process via
                # ``manager.run_one_shot`` instead of the broken HTTP
                # loopback path (RemoteAuthMiddleware rejects /api/* with
                # no Bearer token; there is no loopback bypass).
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
                    # Off-loop: build_and_set_index runs the fastembed/ONNX
                    # tokenizer over every tool description; on cold start
                    # that walk takes 2-5s and would block the main loop
                    # mid-deploy (loop_watchdog reported it: see #stall
                    # 3 + 4 of the deploy path). bootstrap.py already
                    # punted its build_and_set_index to a thread for the
                    # same reason; this second call site was the missing
                    # half of the fix.
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
        # Stash the daemon's event bus on the agent context so runtime
        # paths that don't have access to a FastAPI Request (background
        # activations, cron triggers, channel dispatches) can still emit
        # session-scoped events - notably the ``error`` event on turn
        # failure, which otherwise stayed in the activation table and
        # never reached the client's SSE stream.
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

        # ── Wire Socket.IO bus into preview & widget modules ──
        # So their _publish() also emits events to Socket.IO rooms,
        # enabling Flutter clients to receive preview/widget events
        # without opening a separate SSE connection.
        for mod_name in ("preview", "widget"):
            mod = app_modules.get(mod_name)
            if mod is not None and hasattr(mod, "_event_bus"):
                mod._event_bus = self.event_bus
                mod._bus_app_id = app_id
                logger.info(
                    "bus_wired module=%s app=%s sio=%s",
                    mod_name, app_id, self.event_bus._sio is not None,
                )

        # ── Wire bg notification relay for real-time SSE updates ──
        # The context_builder fires this on every push_module_notification
        # so the frontend sees bg_task_update and memory_update events
        # immediately without polling.
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
                    # op_id = the spawned agent's id so every event of
                    # ONE sub-agent (spawn → progress* → result) lands
                    # under one op_id on the client. op_parent_id is
                    # the coordinator that spawned it, allowing the
                    # client to draw the parent→child tree.
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

            # ── Direct push wake-up bridge ─────────────────────────
            # When a sub-agent reaches a terminal state (completed,
            # failed, timeout, cancelled), wake the coordinator's
            # loop NOW instead of waiting up to 1s for the next
            # polling tick. ``check_notifications`` is idempotent
            # and the polling loop also keeps running as a safety
            # net, so a duplicate trigger is harmless. We capture
            # ``self`` (the manager) and the deploy's ``app_id`` /
            # ``user_id`` in closure so the bridge has everything
            # it needs without going through globals.
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

        # ── Hot reload (dev only) ─────────────────────────────
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

        # Auto-start background mode apps - triggers start listening immediately.
        # Keep a strong reference to the task in self._bg_start_tasks to prevent
        # Python's GC from collecting the pending coroutine (which produces
        # "Task was destroyed but it is pending!" warnings at startup).
        if compiled.execution.mode == "background":
            _bg_task = asyncio.create_task(
                self._auto_start_background(deployed, compiled)
            )
            self._bg_start_tasks.add(_bg_task)
            _bg_task.add_done_callback(self._bg_start_tasks.discard)

        return deployed

    async def _auto_start_background(self, deployed: Any, compiled: Any) -> None:
        """Auto-start a background mode app after deployment.

        Launches trigger listeners (cron, watch, http) or channels module
        listeners. Runs indefinitely until the app is undeployed.

        IMPORTANT: we MUST pass ``runtime_app=deployed`` to
        ``run_background`` so it can locate the ``channels`` module and
        call ``start_listeners()``. Without this, apps that declare their
        triggers under ``modules.channels.config.providers`` (every new
        background app does) never activate - the cron tick stays at
        "ready" and never fires. ``DeployedApp`` has the same ``.modules``
        shape that ``run_background`` expects, so duck typing works.
        """
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

            # Wire hook_runner onto the channels module the same way
            # RuntimeApp._wire_channels_module does it, so the pipeline
            # has a reference for agent_turn activations.
            channels_mod = deployed.modules.get("channels")
            if channels_mod is not None:
                try:
                    channels_mod._runtime_app = deployed  # type: ignore[attr-defined]
                    channels_mod._hook_runner = deployed.hook_runner  # type: ignore[attr-defined]
                except Exception:
                    pass

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
        """Create a sandbox worker for OS-isolated tool execution (standard level).

        The worker only loads modules that touch the OS (filesystem, shell,
        database). The daemon still runs agent_turn and the LLM - the worker
        is just an execution backend for tool calls.
        """
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
        """Create a WorkerPool for per-session OS isolation (strict/maximum level).

        Each session gets its own Landlock sandbox, applied on-demand from a
        warm worker. Workers are recycled after sessions end.

        Resource efficiency for 1000 apps:
        - pool_size=0 by default → no warm workers until first session
        - idle_timeout=60s → workers killed quickly after session ends
        - workspace affinity → sessions sharing workspace share a worker
        - pool_max caps total workers per app
        """
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
        """Get modules that should run in the sandbox."""
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
        """Rebuild agent tool lists after the tool index changed.

        When MCP servers connect post-bootstrap (e.g. after OAuth token
        preload), the index gains new tools.  This method updates each
        AgentContext.tools so the LLM sees them on the next turn.
        """
        from digitorn.modules.context_builder.builder import build_direct_tools
        from digitorn.modules.context_builder.prompt import build_system_prompt
        from digitorn.core.runtime.bootstrap import (
            _build_meta_tools_schema,
            _build_primitive_tools_schema,
            _choose_tool_injection,
        )

        # Per-app ``inject_intent`` flag — same source as the bootstrap
        # path, kept in lockstep so the schemas built at deploy time
        # match what bootstrap rebuilds on restart.
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
        """Get a deployed app or raise - scope-aware.

        Resolves via the public ``get(app_id, user_id=...)`` which
        walks user-scoped → system-scoped → legacy bare key. Callers
        should pass ``user_id`` whenever they have one so a user's
        private deploy shadows the system one.
        """
        deployed = self.get(app_id, user_id=user_id)
        if deployed is None:
            available = list(self._deployed.keys())
            raise RuntimeError(
                f"App '{app_id}' not deployed (available: {available})"
            )
        return deployed

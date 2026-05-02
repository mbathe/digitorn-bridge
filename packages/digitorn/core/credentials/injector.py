"""CredentialInjector - resolve YAML refs at activation time and
write the decrypted fields into the module config tree.

Called from the bootstrap path after Pydantic parsing + module
construction, BEFORE any module's `on_config_update(config)`. The
injector:

  1. Walks the parsed app definition (`AppDefinition`).
  2. For every block carrying a `credential:` ref, resolves it via
     `CredentialStore.get_credential_by_name(...)` at the EXACT
     declared scope (no fallback cascade).
  3. Decrypts the credential fields.
  4. Writes each `field_name → target_path` mapping declared by the
     consumer module's `CredentialSlot.inject` into the module's
     compiled config dict.
  5. Records an `inject` audit row per resolution + registers the
     plaintext value with the LogScrubber so it gets redacted from
     subsequent log lines.
  6. Zeroizes the plaintext from local references after pushing.

If a `required` slot can't be resolved (credential missing, expired,
invalid status), the injector raises `CredentialInjectError` and the
activation aborts cleanly. Non-required slots emit a warning and
leave the consumer config untouched - the module sees the YAML's
inline values (which may be `{{secret.X}}` legacy templates that
the runtime resolver handles separately).

Threading model: synchronous I/O only via the cipher's `decrypt_sync`
on direct-mode providers (env/file). For envelope-mode (KMS)
providers, the injector uses the async API and the bootstrap path
must be in an async context.
"""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.credentials.audit import (
    AuditAction,
    AuditOutcome,
    AuditLog,
)
from digitorn.core.credentials.audit.log import make_record
from digitorn.core.credentials.audit.scrubber import get_global_scrubber
from digitorn.core.credentials.handler import default_registry
from digitorn.core.credentials.schema_yaml import (
    CredentialReference,
    parse_credential_ref,
)
from digitorn.core.credentials.slot import CredentialSlot
from digitorn.core.credentials.store import CredentialStore, Status

logger = logging.getLogger(__name__)


class CredentialInjectError(Exception):
    """Raised when a required credential ref cannot be resolved.

    Carries the block path, the ref, and a structured reason so the
    activation flow can surface a clean error to the user with all the
    info needed to fix it (which scope, which provider, etc.).
    """

    def __init__(
        self,
        *,
        block_path: str,
        ref: str,
        scope: str,
        reason: str,
        provider: str | None = None,
    ) -> None:
        self.block_path = block_path
        self.ref = ref
        self.scope = scope
        self.reason = reason
        # Provider name the YAML's `credential:` block declared
        # (when present). The SSE classifier uses it to render a
        # picker that targets the right provider catalogue entry,
        # not just the bare ref - otherwise the picker's
        # disambiguation falls through to "_extractProviderFromError"
        # heuristics and may pick the wrong handler.
        self.provider = provider
        super().__init__(
            f"{block_path}: cannot resolve credential ref={ref!r} "
            f"scope={scope!r}: {reason}",
        )


class CredentialInjector:
    """Per-activation worker that resolves credential refs into a
    compiled app definition.

    Construct ONE per activation (cheap, holds no state across calls).
    The injector instance carries the (user_id, app_id) tuple of the
    activating session so resolution is scoped correctly.
    """

    def __init__(
        self,
        *,
        store: CredentialStore,
        user_id: str,
        app_id: str,
        audit: AuditLog | None = None,
    ) -> None:
        self._store = store
        self._user_id = user_id
        self._app_id = app_id
        self._audit = audit

    async def inject_app(
        self,
        *,
        app_def: Any,
        module_slots: dict[str, list[CredentialSlot]],
        compiled_blocks: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Resolve every `credential:` ref in `app_def` and inject
        the decrypted fields into the matching `compiled_blocks`
        entries.

        Args:
          app_def: parsed AppDefinition.
          module_slots: `{module_id: [CredentialSlot, ...]}` collected
            from instantiated modules.
          compiled_blocks: `{block_path: <mutable config dict>}` where
            block_path is e.g. ``"brain"``, ``"agents.scout.brain"``,
            ``"modules.git"``. The injector mutates the dicts in place.

        Returns:
          List of injection records (for telemetry / debug). Each
          record carries `block_path`, `ref`, `scope`, `provider`, and
          which target paths were written. NEVER carries plaintext.

        Raises:
          CredentialInjectError on any required slot that fails to
          resolve.
        """
        injections: list[dict[str, Any]] = []

        for block_path, ref_raw, module_id in self._collect_refs(app_def):
            ref = parse_credential_ref(ref_raw)
            if ref is None:
                continue
            slot = self._pick_slot(module_id, module_slots)
            try:
                rec = await self._resolve_and_inject(
                    block_path=block_path,
                    ref=ref,
                    slot=slot,
                    compiled_blocks=compiled_blocks,
                )
                injections.append(rec)
            except CredentialInjectError as exc:
                # Required slot - fatal. Audit + propagate.
                await self._record_audit(
                    block_path=block_path, ref=ref,
                    outcome=AuditOutcome.FAILURE,
                    reason="resolution failed",
                )
                if slot.required:
                    raise
                logger.warning(
                    "credential_inject_optional_skipped block=%s ref=%s reason=%s",
                    block_path, ref.ref, exc.reason,
                )

        return injections

    # ── Private helpers ──────────────────────────────────────────────

    def _collect_refs(
        self, app_def: Any,
    ) -> list[tuple[str, Any, str]]:
        """Yield (block_path, raw_credential_value, module_id) tuples
        for every block in the app that may carry a credential ref."""
        out: list[tuple[str, Any, str]] = []

        # brain
        brain = getattr(app_def, "brain", None)
        if brain is not None:
            cred = getattr(brain, "credential", None)
            if cred is not None:
                out.append(("brain", cred, "llm_provider"))

        # per-agent brain
        for agent in (getattr(app_def, "agents", None) or []):
            agent_id = getattr(agent, "id", "") or "agent"
            ab = getattr(agent, "brain", None)
            if ab is not None:
                cred = getattr(ab, "credential", None)
                if cred is not None:
                    out.append((f"agents.{agent_id}.brain", cred, "llm_provider"))

        # modules
        modules = getattr(app_def, "modules", None) or {}
        if isinstance(modules, dict):
            for mid, blk in modules.items():
                if blk is None:
                    continue
                cred = getattr(blk, "credential", None)
                if cred is not None:
                    out.append((f"modules.{mid}", cred, mid))

        return out

    @staticmethod
    def _pick_slot(
        module_id: str,
        module_slots: dict[str, list[CredentialSlot]],
    ) -> CredentialSlot:
        """Find the matching slot, falling back to a permissive
        synthesised slot if the module didn't declare any."""
        slots = module_slots.get(module_id) or []
        if slots:
            return slots[0]
        return CredentialSlot(
            id="default",
            label=f"{module_id} authentication",
            handler_types=[],
            providers=[],
            scopes_preferred=[],
            scopes_allowed=None,
            inject={},
            required=False,
            help="",
        )

    async def _resolve_and_inject(
        self,
        *,
        block_path: str,
        ref: CredentialReference,
        slot: CredentialSlot,
        compiled_blocks: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Strict-scope vault lookup + decrypt + inject."""
        try:
            row = await self._store.get_credential_by_name(
                name=ref.ref,
                scope=ref.scope,
                user_id=self._user_id,
                app_id=self._app_id,
                decrypt=True,
            )
        except Exception as exc:
            raise CredentialInjectError(
                block_path=block_path, ref=ref.ref, scope=ref.scope,
                reason=f"vault lookup failed: {exc}",
                provider=ref.provider,
            ) from exc

        if row is None:
            raise CredentialInjectError(
                block_path=block_path, ref=ref.ref, scope=ref.scope,
                reason=(
                    f"no credential named {ref.ref!r} found at scope "
                    f"{ref.scope!r} for user {self._user_id!r}"
                    + (f" / app {self._app_id!r}" if "app" in ref.scope else "")
                ),
                provider=ref.provider,
            )

        # Status gate: refuse to inject expired / invalid credentials.
        status = row.get("status") or ""
        if status in (Status.EXPIRED, Status.INVALID, Status.PENDING):
            raise CredentialInjectError(
                block_path=block_path, ref=ref.ref, scope=ref.scope,
                reason=f"credential status is {status!r}; ask the user to refill / refresh",
                provider=ref.provider,
            )

        # Provider sanity check (when YAML specifies one).
        if ref.provider and row.get("provider_name") != ref.provider:
            raise CredentialInjectError(
                block_path=block_path, ref=ref.ref, scope=ref.scope,
                reason=(
                    f"provider mismatch: YAML says {ref.provider!r}, "
                    f"vault entry is {row.get('provider_name')!r}"
                ),
                provider=ref.provider,
            )

        # Pull plaintext fields.
        fields = dict(row.get("fields") or {})

        # Register every plaintext value with the log scrubber so any
        # accidental log line containing it gets redacted.
        scrubber = get_global_scrubber()
        if scrubber is not None:
            for v in fields.values():
                if isinstance(v, str):
                    scrubber.add_dynamic(v, label=ref.ref)

        # Determine the target paths from the slot's inject mapping.
        # When the slot has no explicit inject, fall back to the
        # handler's inject_path_default per FieldSpec.
        inject_map = self._build_inject_map(slot, row.get("provider_type") or "")

        # Mutate the compiled block's config in place.
        target_block = compiled_blocks.get(block_path)
        if target_block is None:
            # Block not pre-registered. Skip but record - the slot's
            # consumer module will surface the missing config later
            # if it actually needed it.
            logger.warning(
                "credential_inject_no_block block=%s ref=%s "
                "(compiled_blocks did not register this path)",
                block_path, ref.ref,
            )
            written: list[str] = []
        else:
            written = self._write_fields_into_block(
                fields=fields,
                inject_map=inject_map,
                block_path=block_path,
                target_block=target_block,
            )

        # Audit success - never include plaintext in the audit row.
        await self._record_audit(
            block_path=block_path, ref=ref,
            outcome=AuditOutcome.SUCCESS,
            reason="injected",
            extra={
                "credential_id": row.get("id"),
                "provider_name": row.get("provider_name"),
                "fields_injected": written,
            },
        )

        # Best-effort zeroize: drop the local reference. Underlying
        # bytes objects in CPython are immutable so this just hands
        # them to the GC sooner.
        del fields

        return {
            "block": block_path,
            "ref": ref.ref,
            "scope": ref.scope,
            "provider": row.get("provider_name"),
            "credential_id": row.get("id"),
            "fields_written": written,
        }

    @staticmethod
    def _build_inject_map(
        slot: CredentialSlot, provider_type: str,
    ) -> dict[str, str]:
        """Return the `field_name → target_path` mapping to use.

        Order of preference:
          1. Explicit `slot.inject` (declared by the module).
          2. Handler default `inject_path_default` per FieldSpec.
          3. Empty (no injection - the resolver wrote nothing).
        """
        if slot.inject:
            return dict(slot.inject)
        try:
            handler = default_registry.get(provider_type)
        except KeyError:
            return {}
        out: dict[str, str] = {}
        for spec in handler.schema_fields():
            tgt = getattr(spec, "inject_path_default", "") or ""
            if tgt:
                out[spec.name] = tgt
        return out

    @staticmethod
    def _write_fields_into_block(
        *,
        fields: dict[str, Any],
        inject_map: dict[str, str],
        block_path: str,
        target_block: dict[str, Any],
    ) -> list[str]:
        """Apply each (field, target) write. Returns the list of
        target paths actually written (for audit/telemetry)."""
        written: list[str] = []
        for field_name, target_template in inject_map.items():
            if field_name not in fields:
                # Credential doesn't carry this field - skip silently.
                continue
            value = fields[field_name]
            if value is None or value == "":
                continue
            # Resolve `{block}` placeholder to the consumer block path.
            target = target_template.replace("{block}", block_path)
            # Strip the `{block}.` prefix to get a path RELATIVE to
            # the block's config dict (the keys we got from the
            # caller are already block-rooted).
            if target.startswith(block_path + "."):
                target = target[len(block_path) + 1:]
            else:
                # No `{block}` prefix - assume the path is relative
                # to the block already.
                pass
            _set_dotted(target_block, target, value)
            written.append(target_template.replace("{block}", block_path))
        return written

    async def _record_audit(
        self,
        *,
        block_path: str,
        ref: CredentialReference,
        outcome: AuditOutcome,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self._audit is None:
            return
        try:
            rec = make_record(
                who=self._user_id,
                action=AuditAction.INJECT,
                on=ref.ref,
                outcome=outcome,
                reason=reason,
                app_id=self._app_id,
                extra={
                    "block": block_path,
                    "scope": ref.scope,
                    "provider": ref.provider or "",
                    **(extra or {}),
                },
            )
            await self._audit.record(rec)
        except Exception as exc:
            logger.warning("credential_inject_audit_failed: %s", exc)


def _set_dotted(target: dict[str, Any], path: str, value: Any) -> None:
    """Set `target[path]` where `path` is a dotted key (e.g.
    `config.api_key`). Creates intermediate dicts as needed."""
    if not path:
        return
    parts = path.split(".")
    cur = target
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value

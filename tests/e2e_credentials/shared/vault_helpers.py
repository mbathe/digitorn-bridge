"""Vault helpers for e2e tests.

Each scenario typically:
  1. Wipes any prior credential under a known name (idempotent).
  2. Creates the credential it needs (api_key, oauth2 token, etc.).
  3. Yields it to the test.
  4. Deletes it on teardown.

Direct DB access is used (rather than HTTP API) because:
  - Some scope/perm combinations (system_wide, per_app_shared)
    require admin perms the dev JWT doesn't have.
  - We control the user_id and don't need auth wrangling.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any


_ENV_KEY = "KghE_laai9HFvcenA__24rr6tl6RUQC86N1RdPWW3Zg="
os.environ.setdefault("DIGITORN_MASTER_KEY", _ENV_KEY)


async def _build_store():
    """Async factory for a CredentialStore against the live daemon DB.

    Uses a direct engine instead of `init_db()` to avoid the
    `_migrate_missing_columns` path that pulls `Base.metadata.sorted_tables`
    and trips on FKs to tables defined in `digitorn_auth.models` (the
    auth service's metadata is separate and not loaded in test process).
    """
    import os as _os
    from sqlalchemy.ext.asyncio import (
        async_sessionmaker, create_async_engine,
    )
    from digitorn.core.config import get_settings
    from digitorn.core.credentials.cipher import VersionedCipher
    from digitorn.core.credentials.master_key.factory import (
        build_provider_from_config,
    )
    from digitorn.core.credentials.store import CredentialStore

    settings = get_settings()
    db_url = (
        getattr(settings.database, "url", "")
        or _os.environ.get("DIGITORN_DATABASE__URL", "")
    )
    # Force the asyncpg driver if the URL is a generic postgres URL.
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(db_url, future=True, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    kms = build_provider_from_config()
    cipher = VersionedCipher(kms)
    return CredentialStore(factory, cipher)


def _user_id_from_jwt() -> str:
    """Decode the active JWT (credentials.json access_token first,
    LocalDeviceAuth.device_token fallback) to extract the user_id
    the daemon sees for our authenticated requests."""
    import base64, json
    tok = ""
    p = os.path.expanduser("~/.digitorn/credentials.json")
    if os.path.isfile(p):
        with open(p) as f:
            tok = json.load(f).get("access_token", "")
    if not tok:
        try:
            from digitorn.core.auth.local_device import LocalDeviceAuth
            tok = LocalDeviceAuth.load().device_token or ""
        except Exception:
            pass
    parts = tok.split(".")
    if len(parts) < 2:
        return ""
    pad = "=" * (-len(parts[1]) % 4)
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    return payload.get("sub", "") or payload.get("user_id", "") or ""


def _http_with_auth(method: str, path: str, body: dict | None = None,
                    *, daemon: str = "http://127.0.0.1:8765") -> tuple[int, dict]:
    """HTTP wrapper for vault operations against the live daemon. We
    use HTTP instead of direct DB to avoid SQLAlchemy metadata issues
    in the test process (the daemon has all models loaded, the test
    process doesn't)."""
    import json as _json
    import urllib.request as _ur
    import urllib.error as _ue
    url = path if path.startswith("http") else f"{daemon}{path}"
    data = _json.dumps(body).encode() if body is not None else None
    req = _ur.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    tok = None
    p = os.path.expanduser("~/.digitorn/credentials.json")
    if os.path.isfile(p):
        with open(p) as f:
            tok = __import__("json").load(f).get("access_token")
    if not tok:
        try:
            from digitorn.core.auth.local_device import LocalDeviceAuth as _LDA
            tok = _LDA.load().device_token
        except Exception:
            pass
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with _ur.urlopen(req, timeout=15) as resp:
            return resp.status, _json.loads(resp.read() or b"{}")
    except _ue.HTTPError as e:
        return e.code, _json.loads(e.read() or b"{}")


def reset_credential(name: str) -> None:
    """Drop every credential row carrying `name` for the JWT user.
    Safe to call before tests so they start from a clean slate."""
    s, d = _http_with_auth("GET", "/api/credentials")
    if s != 200:
        return
    for c in d.get("data", {}).get("credentials", []):
        if c.get("name") == name:
            _http_with_auth("DELETE", f"/api/credentials/{c['id']}")


def create_user_credential(
    *, name: str, provider_name: str, provider_type: str,
    fields: dict[str, Any], scope: str = "per_user",
    label: str | None = None,
) -> str:
    """Create a per_user credential under the JWT user. Returns id."""
    s, d = _http_with_auth("POST", "/api/credentials", {
        "name": name,
        "provider_name": provider_name,
        "provider_type": provider_type,
        "scope": scope,
        "label": label or name,
        "fields": fields,
    })
    if s != 200:
        raise RuntimeError(f"create credential failed: {s} {d}")
    return d.get("data", {}).get("id", "")


def create_system_credential(
    *, name: str, provider_name: str, provider_type: str,
    fields: dict[str, Any], app_id: str | None = None,
    label: str | None = None,
) -> str:
    """Create a system_wide (or per_app_shared if app_id given)."""
    async def _create():
        store = await _build_store()
        row = await store.upsert_system_credential(
            provider_name=provider_name,
            provider_type=provider_type,
            label=label or name,
            app_id=app_id,
            fields=fields,
            name=name,
        )
        return row["id"]
    return asyncio.run(_create())


def delete_credential(credential_id: str) -> None:
    _http_with_auth("DELETE", f"/api/credentials/{credential_id}")


def fetch_existing_field(
    *, provider_name: str, field: str = "api_key",
    scope: str = "system_wide",
) -> str | None:
    """Look up a credential at the given scope/provider and return its
    decrypted field value. Used to "borrow" a real api_key from a
    system-wide row (set up by the operator) so per_user e2e tests
    don't require a separate env var.

    Returns None when no credential matches.
    """
    async def _fetch():
        from digitorn.core.credentials.store import Scope
        store = await _build_store()
        rows = await store.list_credentials(
            user_id=None if scope == Scope.SYSTEM_WIDE else _user_id_from_jwt() or "system",
            scope=scope,
        )
        for r in rows:
            if r.get("provider_name") != provider_name:
                continue
            # Fetch the row WITH decrypted fields by id.
            full = await store.get_credential_by_id(
                r["id"], decrypt=True,
            )
            if not full:
                continue
            v = (full.get("fields") or {}).get(field)
            if v:
                return v
        return None
    return asyncio.run(_fetch())

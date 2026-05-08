# Local-runtime mode: daemon without Postgres

The daemon can run entirely on local files. The four Postgres tables
that drive the session-runtime hot paths are replaced by file-based
backends; Postgres stays only for `users`, `user_sessions`,
`refresh_tokens`, and `user_roles` (auth). When auth is also disabled,
the daemon needs no DB at all.

## Activation (one block of env vars)

```bash
# 1. Session journal + cache (Phase 6 + Phase Compaction).
export DIGITORN_SESSION_STORE_MODE=primary
export DIGITORN_SESSION_STORE_ROOT=$HOME/.digitorn/sessions

# 2. Run tracker -> per-session JSONL on disk (Phase 7.5).
#    Backend already exists at runtime/run_tracker/backends/jsonfile.py.
#    Configure via the daemon's runtime config:
export DIGITORN_RUNTIME__RUN_TRACKER__BACKEND=jsonfile
export DIGITORN_RUNTIME__RUN_TRACKER__PATH=$HOME/.digitorn/runs

# 3. Credentials -> encrypted files per user (Phase 7.2).
#    Wire FileCredentialStore at daemon startup; cipher reuses the
#    same MasterKeyProvider as the DB store.
#    See: digitorn.core.credentials.file_store.FileCredentialStore

# 4. Inbox -> per-user JSON files (Phase 7.4).
#    See: digitorn.core.runtime.session_store.inbox_store.FileInboxStore

# 5. App registry -> ~/.digitorn/apps already mirrored on disk.
#    Phase 7.3 (pending) will make it the primary source.

# 6. Master key for cipher (mandatory in production).
export DIGITORN_GATEWAY_MASTER_KEY=$(openssl rand -base64 32)
```

## Boot flow

```python
# daemon main.py lifespan
from digitorn.core.runtime.session_store import (
    init_session_store, shutdown_session_store,
)
from digitorn.core.credentials.file_store import FileCredentialStore
from digitorn.core.credentials.cipher import VersionedCipher as Cipher
from digitorn.core.credentials.master_key import build_provider_from_config
from digitorn.core.runtime.session_store.inbox_store import FileInboxStore
from digitorn.core.runtime.run_tracker.backends import select_backend
from pathlib import Path


@asynccontextmanager
async def lifespan(app):
    # 1. SessionStore + history.record() bridge
    store = await init_session_store()

    # 2. Credentials store (encrypted local files)
    cipher = Cipher(provider=build_provider_from_config())
    creds = FileCredentialStore(
        root=Path.home() / ".digitorn" / "credentials",
        cipher=cipher,
    )
    app.state.credentials = creds

    # 3. Inbox store
    inbox = FileInboxStore(
        root=Path.home() / ".digitorn" / "inbox",
    )
    app.state.inbox = inbox

    # 4. Run tracker (pluggable backend, configured via env)
    runs = select_backend("jsonfile", config={
        "path": str(Path.home() / ".digitorn" / "runs"),
    })
    await runs.setup()
    app.state.run_tracker = runs

    try:
        yield
    finally:
        await shutdown_session_store(store)
        await runs.teardown()
```

## What stays in Postgres

| Table | Reason |
|-------|--------|
| `users` | Auth identity (email, hashed password, federation links) |
| `user_sessions` | Active session tokens |
| `refresh_tokens` | Token rotation chain |
| `user_roles` | RBAC assignments |

If auth is also disabled (single-user CLI mode), even these four can
be replaced by a flat-file user db. That's outside the daemon's scope
today; the dev-mode CLI already issues local-signed JWTs without DB.

## What this gives you

- **Boot without Postgres reachable**: daemon starts, serves chats,
  sub-agents work. Login is the only feature that needs a network
  hop to the auth service.
- **No history_log writes** in primary mode: the agent loop never
  hits the DB. Sub-µs reads, sub-50µs writes.
- **All projection state in RAM** with periodic disk sync.
- **Full disk durability**: kill -9 loses at most 50 ms of events
  (the DiskFlusher batch interval).
- **Frontend full chronology**: `events.jsonl` is immutable, the
  user sees every event regardless of compaction.
- **Sub-agent isolation**: each agent has its own session dir, runs,
  events. Parallel writes via threadpool.

## What's NOT covered yet

- App registry primary on disk (Phase 7.3) -- `applications`,
  `app_bundles`, `installed_packages` still hit Postgres for now.
  The deployment flow already mirrors to `~/.digitorn/apps/`, but
  the registry source-of-truth is Postgres until 7.3 ships.

- Cross-session queries (user dashboard, search) -- Phase 8 ships
  a SqliteSessionIndex for this, replacing the DB-side queries.

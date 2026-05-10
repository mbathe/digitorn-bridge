"""Session store: in-memory canonical session state with disk-backed
durability. Replaces the Postgres ``history_log`` write path for chat
events while preserving every contract (seq monotonicity, full
persistence, persist-before-broadcast).

Public API entry points:

  * ``Event``                       - the dataclass mirroring history_log columns.
  * ``SessionState``                - the in-memory canonical state.
  * ``InMemorySessionStore``        - the main orchestrator.
  * ``SeqAllocator``                - the monotonic seq generator.
  * ``BlobStore``                   - content-addressed multimedia.

Designed to be self-contained: no Postgres, no daemon imports. Drops in
on top of ``asyncio`` + ``threading`` only. Integration with the
existing daemon happens in later phases.
"""

from digitorn.core.runtime.session_store.types import (
    Event,
    BlobRef,
    ChildAgentRef,
    ParentLink,
    Message,
    ToolCall,
    ToolResult,
    Todo,
    FileState,
    ApprovalRequest,
)
from digitorn.core.runtime.session_store.seq_allocator import SeqAllocator
from digitorn.core.runtime.session_store.session_state import SessionState
from digitorn.core.runtime.session_store.blob_store import BlobStore
from digitorn.core.runtime.session_store.disk_flusher import DiskFlusher
from digitorn.core.runtime.session_store.meta_io import MetaIO
from digitorn.core.runtime.session_store.snapshot import (
    build_snapshot, read_snapshot, write_snapshot,
)
from digitorn.core.runtime.session_store.compaction import (
    Compaction, read_compaction, write_compaction,
)
from digitorn.core.runtime.session_store.token_counter import (
    count_message_tokens, count_text_tokens,
)
from digitorn.core.runtime.session_store.bridge import (
    BridgeMode, SessionStoreBridge,
    get_default_bridge, resolve_mode_from_env, set_default_bridge,
)
from digitorn.core.runtime.session_store.bootstrap import (
    init_session_store, shutdown_session_store,
)
from digitorn.core.runtime.session_store.inbox_store import (
    FileInboxStore, InboxItem,
)
from digitorn.core.runtime.session_store.app_registry import (
    AppBundle, Application, FileAppRegistry,
)
from digitorn.core.runtime.session_store.session_index import (
    SessionIndex, SessionSummary, SqliteSessionIndex,
)
from digitorn.core.runtime.session_store.verify import (
    DiffReport, diff_event_lists, diff_jsonl_files,
    diff_stores_for_session,
)
from digitorn.core.runtime.session_store.reader import (
    latest_seq, load_messages_for_llm, load_snapshot,
    replay_events_since, session_summary,
)
from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.job_store import FileJobStore

__all__ = [
    "Event",
    "BlobRef",
    "ChildAgentRef",
    "ParentLink",
    "Message",
    "ToolCall",
    "ToolResult",
    "Todo",
    "FileState",
    "ApprovalRequest",
    "SeqAllocator",
    "SessionState",
    "BlobStore",
    "DiskFlusher",
    "MetaIO",
    "build_snapshot",
    "read_snapshot",
    "write_snapshot",
    "Compaction",
    "read_compaction",
    "write_compaction",
    "count_message_tokens",
    "count_text_tokens",
    "BridgeMode",
    "SessionStoreBridge",
    "get_default_bridge",
    "set_default_bridge",
    "resolve_mode_from_env",
    "latest_seq",
    "load_messages_for_llm",
    "load_snapshot",
    "replay_events_since",
    "session_summary",
    "InMemorySessionStore",
    "FileJobStore",
    "init_session_store",
    "shutdown_session_store",
    "FileInboxStore",
    "InboxItem",
    "AppBundle",
    "Application",
    "FileAppRegistry",
    "SessionIndex",
    "SessionSummary",
    "SqliteSessionIndex",
    "DiffReport",
    "diff_event_lists",
    "diff_jsonl_files",
    "diff_stores_for_session",
]

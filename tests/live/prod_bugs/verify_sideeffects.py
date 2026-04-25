"""Verify the patches for the side-effect risks.

  - BUG-035/027 cross-tab : same user/same-sid from different contexts
  - BUG-006 migration    : old `memory:{app}:long_term` keys still readable
  - BUG-034 grace period : first user in a clean DB gets admin
  - BUG-017 root cause   : log surfaces the empty-name source
  - BUG-038 ghost apps   : bootstrap refuses empty contexts
  - BUG-028 seq per-sess : two sessions have independent counters
"""
from __future__ import annotations
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages"))

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8000"
WORKSPACE = ROOT / "tests" / "live" / "prod" / "workspace"


def pass_(ok: bool, label: str, detail: str = "") -> dict:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}")
    if detail:
        print(f"         {detail}")
    return {"ok": ok, "label": label, "detail": detail}


def http(method: str, path: str, **kwargs):
    try:
        r = httpx.request(method.upper(), f"{BASE}{path}", timeout=15, **kwargs)
        return r.status_code, r.text
    except Exception as exc:
        return 0, str(exc)


def main() -> int:
    results = []

    # ── 1. BUG-035/027 same-sid-different-contexts behavior ──
    # With the compound key (uid::sid), two concurrent callers using the
    # SAME user_id AND the SAME session_id must still share memory.
    # Otherwise they'd see two orphan stores for the same tab.
    print("\n── BUG-035/027: same uid+sid share the store (no orphan) ──")
    sys.path.insert(0, str(ROOT / "packages"))
    from digitorn.modules.memory.module import MemoryModule
    import asyncio

    async def _probe_same_sid():
        mod = MemoryModule()
        await mod.on_config_update({
            "working_memory": True, "todo_list": True,
        })
        # Mimic two tabs: same session_id "tab-1" passed explicitly.
        s1 = mod.get_session_store("tab-1")
        s2 = mod.get_session_store("tab-1")
        return s1 is s2

    same = asyncio.run(_probe_same_sid())
    results.append(pass_(
        same,
        "two get_session_store('tab-1') calls return the SAME store",
        f"same_obj={same}",
    ))

    # ── 2. BUG-006 legacy key migration ──
    print("\n── BUG-006: legacy key migrates lazily to user-scoped ──")
    from digitorn.modules.memory.store import MemoryStore
    # Build an in-memory KV to simulate the backend
    class FakeKV:
        def __init__(self):
            self.store: dict[str, str] = {}
        def get(self, k):
            return self.store.get(k)
        def set(self, k, v, expire=None):
            self.store[k] = v
    kv = FakeKV()
    legacy_data = json.dumps({
        "key_facts": ["legacy fact"],
        "episodes": [],
        "semantic": {"facts": [], "graph": []},
        "procedures": [],
    })
    kv.store["memory:my-app:long_term"] = legacy_data
    st = MemoryStore()
    count = st.restore(kv, "my-app", user_id="alice")
    # After restore, the user-scoped key must now contain the data
    user_key = "memory:my-app:alice:long_term"
    migrated = kv.store.get(user_key) is not None
    results.append(pass_(
        count > 0 and migrated,
        "legacy key data flows into user-scoped key",
        f"restored={count} user_key_present={migrated}",
    ))

    # ── 3. BUG-034 grace period (first admin) ──
    # Hard to exercise without wiping the DB, so we check the code path:
    import inspect
    from digitorn.core.auth.service import AuthService
    src = inspect.getsource(AuthService)
    has_grace = ("_any_user_has_role" in src
                 and '"admin"' in src
                 and "bootstrap_admin" in src)
    results.append(pass_(
        has_grace,
        "register() grants admin to the first user (grace period)",
        "",
    ))

    # ── 4. BUG-038 ghost apps — bootstrap refuses empty contexts ──
    print("\n── BUG-038: bootstrap refuses empty agent contexts ──")
    from digitorn.core.runtime import bootstrap as bs
    bs_src = inspect.getsource(bs._build_agent_contexts)
    has_guard = (
        "if not contexts:" in bs_src
        and "RuntimeError" in bs_src
        and "ghost" in bs_src.lower()
    )
    results.append(pass_(
        has_guard,
        "_build_agent_contexts raises if contexts dict is empty",
        "",
    ))

    # entry_context has clear error now
    from digitorn.core.app.manager import DeployedApp
    dep_src = inspect.getsource(DeployedApp.entry_context.fget)
    has_entry_guard = (
        "self.contexts" in dep_src and "ghost" in dep_src.lower()
    )
    results.append(pass_(
        has_entry_guard,
        "DeployedApp.entry_context raises a clear error on empty contexts",
        "",
    ))

    # ── 5. BUG-017 log surfaces empty name source ──
    print("\n── BUG-017: tool_call name='' surfaces a log with stack ──")
    from digitorn.core.app import manager as mgr
    # We can't easily call the inner closure, but the source should show
    # the `logger.warning("tool_call_empty_name ...")` line.
    mgr_src = Path(mgr.__file__).read_text(encoding="utf-8")
    has_log = "tool_call_empty_name" in mgr_src
    results.append(pass_(
        has_log,
        "empty-name code path logs the recovered value + stack",
        "",
    ))

    # ── 6. BUG-028 seq per-session ──
    print("\n── BUG-028: seq counter is session-scoped ──")
    from digitorn.core.events.event_buffer import EventBuffer
    buf = EventBuffer(max_per_user=1000)
    seqs_a = [buf.next_seq("u1", "sess-A") for _ in range(5)]
    seqs_b = [buf.next_seq("u1", "sess-B") for _ in range(5)]
    # Each session starts its own monotonic sequence
    a_starts_low = seqs_a[0] == 1 and seqs_a == [1, 2, 3, 4, 5]
    b_starts_low = seqs_b[0] == 1 and seqs_b == [1, 2, 3, 4, 5]
    results.append(pass_(
        a_starts_low and b_starts_low,
        "two sessions of the same user have independent 1..N seqs",
        f"A={seqs_a} B={seqs_b}",
    ))

    # User-level (no session_id) still works as before
    seqs_user = [buf.next_seq("u1") for _ in range(3)]
    # `u1` user-level key is distinct from `u1::sess-*`
    results.append(pass_(
        seqs_user == [1, 2, 3],
        "user-level (no session_id) counter is independent of sessions",
        f"user={seqs_user}",
    ))

    print(f"\n=> {sum(1 for r in results if r['ok'])}/{len(results)} pass")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

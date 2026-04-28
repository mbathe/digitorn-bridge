"""BUG-070 to BUG-076: /sessions/{sid}/* lockdown.

Any ``/sessions/{sid}/*`` handler must refuse:
  * anonymous callers (BUG-073: ``/events`` was reachable without a token)
  * cross-user callers (BUG-070 ``/events``, BUG-071 ``/abort``,
    BUG-072 ``/messages``, BUG-074 ``/fork``, BUG-075 ``/export``,
    BUG-076 ``/queue`` ``/context-breakdown`` ``/workspace``)

The guard helpers in ``api/apps.py`` centralise that check; this test
verifies each branch without spinning up the full app.
"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from fastapi import HTTPException  # noqa: E402

from digitorn.core.api.apps import (  # noqa: E402
    _require_session_access,
    _require_session_create_or_owner,
)


def _req(uid: str | None, manager: object) -> SimpleNamespace:
    state = SimpleNamespace(user_id=uid)
    # _get_manager reads request.app.state.app_manager
    return SimpleNamespace(
        state=state,
        app=SimpleNamespace(state=SimpleNamespace(app_manager=manager)),
    )


def _make_manager(owner_uid: str | None) -> MagicMock:
    """Stand-in AppManager where ``get_session`` only matches
    ``owner_uid``; any other caller gets ``None`` (the real store
    enforces this at the key level).
    """
    m = MagicMock()
    m._session_store = MagicMock()
    m._session_store.get_any_owner = MagicMock(return_value=owner_uid)

    async def _get_session(app_id, sid, user_id=None):
        return SimpleNamespace(user_id=owner_uid, app_id=app_id, session_id=sid) \
            if user_id == owner_uid else None

    m.get_session = AsyncMock(side_effect=_get_session)
    return m


async def run() -> int:
    failures: list[str] = []

    # 1. anonymous → 401 on strict check
    mgr = _make_manager("userA")
    try:
        await _require_session_access(_req("anonymous", mgr), "app", "sid")
    except HTTPException as exc:
        if exc.status_code != 401:
            failures.append(f"anon: expected 401 got {exc.status_code}")
    else:
        failures.append("anon: should raise 401")

    # 2. cross-user → 404 on strict check
    try:
        await _require_session_access(_req("userB", mgr), "app", "sid")
    except HTTPException as exc:
        if exc.status_code != 404:
            failures.append(f"cross-user: expected 404 got {exc.status_code}")
    else:
        failures.append("cross-user: should raise 404")

    # 3. owner → returns the session
    sess = await _require_session_access(_req("userA", mgr), "app", "sid")
    if sess is None or sess.user_id != "userA":
        failures.append(f"owner: expected session, got {sess!r}")

    # 4. create-or-owner: anonymous → 401
    try:
        await _require_session_create_or_owner(_req("anonymous", mgr), "app", "sid")
    except HTTPException as exc:
        if exc.status_code != 401:
            failures.append(f"create-or-owner anon: expected 401 got {exc.status_code}")
    else:
        failures.append("create-or-owner anon: should raise 401")

    # 5. create-or-owner: userB and existing session owned by userA → 404
    try:
        await _require_session_create_or_owner(_req("userB", mgr), "app", "sid")
    except HTTPException as exc:
        if exc.status_code != 404:
            failures.append(f"create-or-owner cross: expected 404 got {exc.status_code}")
    else:
        failures.append("create-or-owner cross: should raise 404")

    # 6. create-or-owner: caller owns → passes, returns session
    sess2 = await _require_session_create_or_owner(
        _req("userA", mgr), "app", "sid",
    )
    if sess2 is None or sess2.user_id != "userA":
        failures.append(f"create-or-owner own: expected session, got {sess2!r}")

    # 7. create-or-owner: fresh sid (no owner) → None (create path)
    mgr_fresh = _make_manager(None)  # get_any_owner returns None → fresh
    fresh = await _require_session_create_or_owner(
        _req("userA", mgr_fresh), "app", "newsid",
    )
    if fresh is not None:
        failures.append(f"create-or-owner fresh: expected None got {fresh!r}")

    if failures:
        print("FAIL - session access guard:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - session access guards reject anon + cross-user, allow owner + fresh-sid-create")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))

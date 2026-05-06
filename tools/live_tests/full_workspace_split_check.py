"""End-to-end verification of the workspace/workdir split + hidden paths.

Scenarios:

  S1.  Bundled SDK auto-attach: digitorn-react-sandbox session creation
       -> /web-preview returns the bundled URL, /web-static serves HTML.

  S2.  Workspace auto-created under ~/.digitorn even with no workdir:
       session.workspace points to ~/.digitorn/workspaces/{app}/{sid}/.

  S3.  When NO workdir supplied, workdir == workspace (legacy behaviour).

  S4.  When workdir IS supplied, daemon stores both:
       session.workspace = ~/.digitorn/.../{sid}/  (auto)
       session.workdir   = user-supplied dir
       The workdir gets ZERO daemon files (.digitorn/, __sdk__/, state.json).

  S5.  Agent tools (WsWrite/WsRead/WsGlob/WsGrep/WsEdit/WsDelete) refuse
       hidden namespaces (__sdk__/, .app/, .digitorn/).

  S6.  Filesystem tools (Read/Write/Edit/Glob/Grep) refuse the same.

  S7.  HTTP routes used by the SDK iframe BYPASS the hidden filter:
       PUT /workspace/files/__sdk__/prefs.json succeeds, file lands in
       the daemon workspace dir (NOT the workdir).

  S8.  SDK can write/read regular files via HTTP -> land in workdir.

  S9.  /preview snapshot exposes session metadata: created_at,
       last_active_at, turn_count, is_first_visit, workspace, workdir.

  S10. PreviewProxy attach still works end-to-end (port pre-check +
       auto-wait + lookup).

Reports a final PASS/FAIL summary.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from digitorn.testing.client import DevClient


_DAEMON = "http://127.0.0.1:8000"
_PASSWORD = "Px12345abcd!"


class Reporter:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, ok, detail))
        tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}] {name}{(' - ' + detail) if detail else ''}")

    def summary(self) -> int:
        passed = sum(1 for _, ok, _ in self.results if ok)
        total = len(self.results)
        print()
        print(f"=== {passed}/{total} scenarios passed ===")
        if passed != total:
            print("Failed:")
            for name, ok, detail in self.results:
                if not ok:
                    print(f"  - {name}: {detail}")
        return 0 if passed == total else 1


def _login(daemon_url: str, email: str, password: str) -> str:
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        r = c.post(f"{daemon_url}/auth/login",
                   json={"email": email, "password": password})
        if r.status_code == 401:
            uname = email.split("@", 1)[0].replace(".", "_").replace("-", "_")
            reg = c.post(f"{daemon_url}/auth/register",
                         json={"email": email, "password": password, "username": uname})
            if reg.status_code not in (200, 201):
                raise RuntimeError(f"register {reg.status_code}: {reg.text[:200]}")
            r = c.post(f"{daemon_url}/auth/login",
                       json={"email": email, "password": password})
        if r.status_code != 200:
            raise RuntimeError(f"login {r.status_code}: {r.text[:200]}")
        return r.json().get("access_token") or ""


def _exec_tool(client: DevClient, app_id: str, sid: str,
               tool: str, params: dict) -> dict:
    r = client._post(
        f"/api/apps/{app_id}/tools/{tool}/execute",
        json={"session_id": sid, "params": params},
    )
    try:
        return r.json()
    except Exception:
        return {"success": False, "error": r.text[:300], "status": r.status_code}


def _create_session(client: DevClient, app_id: str,
                    workdir: str | None = None) -> dict:
    body: dict[str, Any] = {"message": "Reply with 'ok' and stop. Do not call any tool."}
    if workdir is not None:
        body["workdir"] = str(workdir)
    r = client._post(f"/api/apps/{app_id}/sessions", json=body)
    if r.status_code != 200:
        raise RuntimeError(f"create_session {r.status_code}: {r.text[:300]}")
    return r.json().get("data") or {}


def _get_snapshot(client: DevClient, app_id: str, sid: str) -> dict:
    r = client._get(f"/api/apps/{app_id}/sessions/{sid}/preview")
    if r.status_code != 200:
        raise RuntimeError(f"snapshot {r.status_code}: {r.text[:200]}")
    body = r.json()
    return body.get("data") or body


# -- Scenarios ---------------------------------------------------------


def s1_bundled_auto_attach(client: DevClient, app: str, rep: Reporter) -> None:
    print("S1. Bundled SDK auto-attach")
    sess = _create_session(client, app)
    sid = sess["session_id"]
    time.sleep(0.5)
    r = client._get(
        f"/api/apps/{app}/web-preview?session_id={sid}&name=default"
    )
    rep.add("S1.1 lookup returns 200", r.status_code == 200,
            f"got {r.status_code}")
    if r.status_code == 200:
        payload = r.json()
        rep.add("S1.2 type is bundled", payload.get("type") == "bundled",
                f"got {payload.get('type')}")
        url = payload.get("url", "")
        rep.add("S1.3 url uses /web-static", "web-static" in url, url[:80])
        # Fetch the URL and confirm HTML
        r2 = client._get(url)
        rep.add("S1.4 web-static serves HTML",
                r2.status_code == 200 and "<!doctype" in r2.text.lower(),
                f"status={r2.status_code} bytes={len(r2.text)}")
    return sid


def s2_workspace_auto(client: DevClient, app: str, rep: Reporter) -> None:
    print("\nS2. Workspace auto-created under ~/.digitorn")
    sess = _create_session(client, app)
    sid = sess["session_id"]
    snap = _get_snapshot(client, app, sid)
    s = snap.get("session") or {}
    workspace = s.get("workspace", "")
    rep.add("S2.1 session.workspace is non-empty", bool(workspace), workspace)
    expected_prefix = str(Path.home() / ".digitorn" / "workspaces")
    rep.add(
        "S2.2 workspace under ~/.digitorn",
        workspace.lower().startswith(expected_prefix.lower()),
        f"workspace={workspace}",
    )
    rep.add(
        "S2.3 workspace dir exists on disk",
        os.path.isdir(workspace),
        workspace,
    )


def s3_no_workdir_means_workdir_eq_workspace(
    client: DevClient, app: str, rep: Reporter,
) -> None:
    print("\nS3. No workdir -> workdir == workspace (legacy)")
    sess = _create_session(client, app)
    sid = sess["session_id"]
    snap = _get_snapshot(client, app, sid)
    s = snap.get("session") or {}
    rep.add(
        "S3.1 workdir == workspace when not supplied",
        s.get("workdir") == s.get("workspace") and bool(s.get("workspace")),
        f"workspace={s.get('workspace')} workdir={s.get('workdir')}",
    )


def s4_workdir_supplied_split(
    client: DevClient, app: str, rep: Reporter,
) -> str:
    print("\nS4. Custom workdir -> split workspace != workdir")
    user_workdir = Path.home() / ".digitorn" / "test-workspaces" / f"split-{uuid.uuid4().hex[:6]}"
    user_workdir.mkdir(parents=True, exist_ok=True)
    sess = _create_session(client, app, workdir=str(user_workdir))
    sid = sess["session_id"]
    snap = _get_snapshot(client, app, sid)
    s = snap.get("session") or {}
    workspace = s.get("workspace", "")
    workdir = s.get("workdir", "")
    # The frontend-facing ``workspace`` field carries the workdir value
    # (agent-facing path); daemon-private path is exposed separately
    # under ``daemon_workspace`` for diagnostics / tests / SDK helpers.
    daemon_ws = s.get("daemon_workspace", "")
    rep.add(
        "S4.1 daemon_workspace under ~/.digitorn",
        daemon_ws.lower().startswith(
            str(Path.home() / ".digitorn" / "workspaces").lower(),
        ),
        f"daemon_workspace={daemon_ws}",
    )
    rep.add(
        "S4.2 workdir matches user-supplied",
        os.path.normcase(workdir) == os.path.normcase(str(user_workdir)),
        f"workdir={workdir} expected={user_workdir}",
    )
    rep.add(
        "S4.3 daemon_workspace != workdir",
        os.path.normcase(daemon_ws) != os.path.normcase(workdir),
        "split is real",
    )
    return sid


def s5_agent_workspace_hidden(client: DevClient, app: str, rep: Reporter) -> None:
    print("\nS5. Agent workspace tools refuse hidden paths")
    # Workspace module needed for WsWrite/WsRead - ``agent-with-preview``
    # is the test bed (declared workspace module + workdir_mode required).
    user_workdir = Path.home() / ".digitorn" / "test-workspaces" / f"s5-{uuid.uuid4().hex[:6]}"
    user_workdir.mkdir(parents=True, exist_ok=True)
    sess = _create_session(client, app, workdir=str(user_workdir))
    sid = sess["session_id"]
    time.sleep(0.5)

    # WsWrite to __sdk__/ -> refused
    res = _exec_tool(client, app, sid, "WsWrite", {
        "path": "__sdk__/foo.json", "content": "test",
    })
    rep.add(
        "S5.1 WsWrite to __sdk__/* refused",
        res.get("success") is False
        and "hidden namespace" in (res.get("error") or "").lower(),
        (res.get("error") or "")[:120],
    )

    # WsWrite to regular path -> ok
    res = _exec_tool(client, app, sid, "WsWrite", {
        "path": "regular.txt", "content": "hello",
    })
    rep.add(
        "S5.2 WsWrite regular path OK",
        res.get("success") is True,
        (res.get("error") or "no error"),
    )

    # WsRead __sdk__/ -> not found (even if file exists via SDK route)
    res = _exec_tool(client, app, sid, "WsRead", {
        "path": "__sdk__/anything.json",
    })
    rep.add(
        "S5.3 WsRead __sdk__/* returns not-found",
        res.get("success") is False,
        (res.get("error") or "")[:120],
    )


def s6_filesystem_hidden(client: DevClient, app: str, rep: Reporter) -> None:
    print("\nS6. Filesystem tools refuse hidden paths")
    # ``agent-with-preview`` has ``workdir_mode: required`` so we MUST
    # supply a workdir at creation time. Use a throwaway temp dir for
    # the test - hidden-path enforcement doesn't depend on its contents.
    user_workdir = Path.home() / ".digitorn" / "test-workspaces" / f"s6-{uuid.uuid4().hex[:6]}"
    user_workdir.mkdir(parents=True, exist_ok=True)
    sess = _create_session(client, app, workdir=str(user_workdir))
    sid = sess["session_id"]
    time.sleep(0.5)

    res = _exec_tool(client, app, sid, "Write", {
        "file_path": "__sdk__/secret.txt", "content": "x",
    })
    rep.add(
        "S6.1 Write to __sdk__/* refused",
        res.get("success") is False
        and "hidden namespace" in (res.get("error") or "").lower(),
        (res.get("error") or "")[:120],
    )

    res = _exec_tool(client, app, sid, "Read", {
        "file_path": "__sdk__/whatever.json",
    })
    err_lower = (res.get("error") or "").lower()
    rep.add(
        "S6.2 Read __sdk__/* returns not-found",
        res.get("success") is False
        and ("does not exist" in err_lower or "not found" in err_lower),
        (res.get("error") or "")[:120],
    )


def s7_sdk_writes_hidden_to_workspace(
    client: DevClient, app: str, rep: Reporter,
) -> None:
    print("\nS7. SDK writes to __sdk__/ -> land in workspace, not workdir")
    user_workdir = Path.home() / ".digitorn" / "test-workspaces" / f"sdk-{uuid.uuid4().hex[:6]}"
    user_workdir.mkdir(parents=True, exist_ok=True)
    sess = _create_session(client, app, workdir=str(user_workdir))
    sid = sess["session_id"]
    snap = _get_snapshot(client, app, sid)
    s = snap.get("session") or {}
    workspace = s.get("daemon_workspace", "")
    workdir = s.get("workdir", "")

    # SDK writes to a hidden namespace via PUT /workspace/files/{path}
    payload = {"prefs": "value", "ts": int(time.time())}
    r = client._post if False else client._put if hasattr(client, "_put") else None
    # DevClient may not have _put - use raw httpx through the session
    H = {"Authorization": f"Bearer {client._token}"} if hasattr(client, "_token") else {}
    with httpx.Client(timeout=15) as c:
        # Use the session token from DevClient
        token = getattr(client, "_token", None) or client.__dict__.get("_token", "")
        if not token:
            # Fallback: read auth header off DevClient internals
            try:
                token = client._client.headers.get("authorization", "").replace("Bearer ", "")
            except Exception:
                token = ""
        if not token:
            rep.add("S7.0 token introspection", False, "no token reachable")
            return
        H = {"Authorization": f"Bearer {token}"}
        r = c.put(
            f"{_DAEMON}/api/apps/{app}/sessions/{sid}/workspace/files/__sdk__/test-prefs.json",
            json={"content": json.dumps(payload), "auto_approve": True},
            headers=H,
        )
    rep.add(
        "S7.1 PUT workspace/files/__sdk__/* -> 200",
        r.status_code == 200,
        f"got {r.status_code}: {r.text[:200]}",
    )
    if r.status_code == 200:
        # Verify physical location: must be in workspace, NOT workdir
        ws_path = Path(workspace) / "__sdk__" / "test-prefs.json"
        wd_path = Path(workdir) / "__sdk__" / "test-prefs.json"
        rep.add(
            "S7.2 file lands in workspace dir",
            ws_path.is_file(),
            f"expected at {ws_path}",
        )
        rep.add(
            "S7.3 file does NOT pollute workdir",
            not wd_path.is_file(),
            f"workdir clean: {wd_path}",
        )


def s8_sdk_writes_regular_to_workdir(
    client: DevClient, app: str, rep: Reporter,
) -> None:
    print("\nS8. SDK writes to regular path -> land in workdir")
    user_workdir = Path.home() / ".digitorn" / "test-workspaces" / f"reg-{uuid.uuid4().hex[:6]}"
    user_workdir.mkdir(parents=True, exist_ok=True)
    sess = _create_session(client, app, workdir=str(user_workdir))
    sid = sess["session_id"]
    snap = _get_snapshot(client, app, sid)
    s = snap.get("session") or {}
    workdir = s.get("workdir", "")
    workspace = s.get("workspace", "")

    token = getattr(client, "_token", "") or ""
    if not token:
        try:
            token = client._client.headers.get("authorization", "").replace("Bearer ", "")
        except Exception:
            token = ""
    if not token:
        rep.add("S8.0 token introspection", False, "no token reachable")
        return
    H = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=15) as c:
        r = c.put(
            f"{_DAEMON}/api/apps/{app}/sessions/{sid}/workspace/files/README.md",
            json={"content": "# Test", "auto_approve": True},
            headers=H,
        )
    rep.add(
        "S8.1 PUT regular file -> 200",
        r.status_code == 200,
        f"got {r.status_code}",
    )
    if r.status_code == 200:
        rep.add(
            "S8.2 regular file lands in workdir",
            (Path(workdir) / "README.md").is_file(),
            f"expected at {workdir}/README.md",
        )


def s9_session_metadata(client: DevClient, app: str, rep: Reporter) -> None:
    print("\nS9. /preview snapshot exposes session metadata")
    sess = _create_session(client, app)
    sid = sess["session_id"]
    snap = _get_snapshot(client, app, sid)
    s = snap.get("session") or {}
    rep.add("S9.1 has session_id", bool(s.get("session_id")), s.get("session_id", ""))
    rep.add("S9.2 has app_id", bool(s.get("app_id")), s.get("app_id", ""))
    rep.add("S9.3 has created_at", isinstance(s.get("created_at"), (int, float))
            and s.get("created_at", 0) > 0, str(s.get("created_at")))
    rep.add("S9.4 has last_active_at", isinstance(s.get("last_active_at"), (int, float)),
            str(s.get("last_active_at")))
    rep.add("S9.5 has turn_count", isinstance(s.get("turn_count"), int), str(s.get("turn_count")))
    rep.add("S9.6 has is_first_visit", isinstance(s.get("is_first_visit"), bool),
            str(s.get("is_first_visit")))
    rep.add("S9.7 has workspace path", bool(s.get("workspace")), str(s.get("workspace")))
    rep.add("S9.8 has workdir path", bool(s.get("workdir")), str(s.get("workdir")))


def s10_preview_proxy(client: DevClient, rep: Reporter) -> None:
    print("\nS10. PreviewProxy agent path still works")
    app = "agent-with-preview"
    yaml_path = Path(
        "c:/Users/ASUS/Documents/digitorn-bridge/examples/"
        "agent-with-preview/app.yaml"
    )
    try:
        client.deploy(str(yaml_path), force=True)
    except Exception as exc:
        rep.add("S10.0 deploy agent-with-preview", False, str(exc)[:120])
        return
    rep.add("S10.0 deploy agent-with-preview", True, "")

    user_workdir = Path.home() / ".digitorn" / "test-workspaces" / f"proxy-{uuid.uuid4().hex[:6]}"
    user_workdir.mkdir(parents=True, exist_ok=True)
    sess = _create_session(client, app, workdir=str(user_workdir))
    sid = sess["session_id"]
    time.sleep(1.0)

    # Spawn http.server in workdir on a fresh port
    port = 47833 + (os.getpid() % 50)
    (user_workdir / "hello.html").write_text(
        "<!doctype html><h1>proxy ok</h1>", encoding="utf-8",
    )
    cmd = f"cd '{user_workdir}' && python -m http.server {port} --bind 127.0.0.1"
    bash_res = _exec_tool(client, app, sid, "Bash", {
        "command": cmd, "run_in_background": True,
    })
    rep.add("S10.1 Bash spawn http.server",
            bool(bash_res.get("success")),
            (bash_res.get("error") or "")[:120])
    bash_task_id = (bash_res.get("data") or {}).get("task_id") or ""

    proxy_res = _exec_tool(client, app, sid, "PreviewProxy", {
        "port": port, "path": "/hello.html", "bash_task_id": bash_task_id,
    })
    rep.add("S10.2 PreviewProxy attach",
            bool(proxy_res.get("success")),
            (proxy_res.get("error") or "")[:120])

    if proxy_res.get("success"):
        url = (proxy_res.get("data") or {}).get("iframe_url") or ""
        rep.add("S10.3 iframe_url has the right path",
                "/hello.html" in url, url[:80])
        try:
            r = httpx.get(url, timeout=5.0)
            rep.add("S10.4 direct fetch returns the page",
                    r.status_code == 200 and "proxy ok" in r.text,
                    f"status={r.status_code}")
        except Exception as exc:
            rep.add("S10.4 direct fetch", False, str(exc)[:120])

    # Cleanup the dev server
    try:
        _exec_tool(client, app, sid, "Bash",
                   {"task_id": bash_task_id, "kill": True})
    except Exception:
        pass


def s11_workdir_clean(client: DevClient, app: str, rep: Reporter) -> None:
    print("\nS11. workdir stays clean (no .digitorn / __sdk__ pollution)")
    user_workdir = Path.home() / ".digitorn" / "test-workspaces" / f"clean-{uuid.uuid4().hex[:6]}"
    user_workdir.mkdir(parents=True, exist_ok=True)
    sess = _create_session(client, app, workdir=str(user_workdir))
    sid = sess["session_id"]
    time.sleep(1.0)

    # Have the agent write a regular file via WsWrite
    _exec_tool(client, app, sid, "WsWrite", {
        "path": "src/main.tsx",
        "content": "// hello\nexport default function () { return null; }",
    })
    time.sleep(1.0)

    # Snapshot to get workspace/workdir. Use ``daemon_workspace`` to
    # locate the daemon-private dir - the public ``workspace`` field
    # now mirrors ``workdir`` for frontend compatibility.
    snap = _get_snapshot(client, app, sid)
    s = snap.get("session") or {}
    workdir = Path(s.get("workdir", str(user_workdir)))
    workspace = Path(s.get("daemon_workspace", ""))

    # The user-visible workdir must be CLEAN of daemon-internal dirs
    pollution = []
    for forbidden in ("__sdk__", ".app", ".digitorn"):
        p = workdir / forbidden
        if p.exists():
            pollution.append(str(p))
    rep.add(
        "S11.1 workdir has no daemon-internal dirs",
        not pollution,
        ("polluted: " + ", ".join(pollution)) if pollution else "clean",
    )
    # The regular file MUST be in workdir
    rep.add(
        "S11.2 src/main.tsx in workdir",
        (workdir / "src" / "main.tsx").is_file(),
        f"expected at {workdir}/src/main.tsx",
    )
    # state.json MUST be in workspace, not workdir
    rep.add(
        "S11.3 state.json in workspace, not workdir",
        (
            (workspace / ".digitorn" / "sessions" / sid / "state.json").is_file()
            and not (workdir / ".digitorn").exists()
        ),
        f"workspace={workspace}",
    )


# -- Main --------------------------------------------------------------


def main() -> int:
    email = os.environ.get(
        "DEV_EMAIL", f"split-{uuid.uuid4().hex[:8]}@example.com",
    )
    token = _login(_DAEMON, email, os.environ.get("DEV_PASSWORD", _PASSWORD))
    print(f"[setup] logged in as {email}")
    client = DevClient.with_token(token, daemon_url=_DAEMON)
    # Stash the token on the client so scenarios can rebuild Authorization headers.
    client._token = token  # type: ignore[attr-defined]

    # Force-redeploy both SDK builtins from source so tests run against
    # the current YAML + bundled dist, not whatever stale version might
    # be on disk from a previous session.
    for app, yaml_path in [
        ("digitorn-react-sandbox",
         "c:/Users/ASUS/Documents/digitorn-bridge/packages/digitorn/builtins/digitorn-react-sandbox/app.yaml"),
        ("agent-with-preview",
         "c:/Users/ASUS/Documents/digitorn-bridge/examples/agent-with-preview/app.yaml"),
    ]:
        try:
            client.deploy(yaml_path, force=True)
            print(f"[setup] redeployed {app}")
        except Exception as exc:
            print(f"[setup] deploy {app}: {exc}")

    rep = Reporter()
    try:
        s1_bundled_auto_attach(client, "digitorn-react-sandbox", rep)
        s2_workspace_auto(client, "digitorn-react-sandbox", rep)
        s3_no_workdir_means_workdir_eq_workspace(client, "digitorn-react-sandbox", rep)
        s4_workdir_supplied_split(client, "digitorn-react-sandbox", rep)
        s5_agent_workspace_hidden(client, "agent-with-preview", rep)
        s6_filesystem_hidden(client, "agent-with-preview", rep)
        s7_sdk_writes_hidden_to_workspace(client, "agent-with-preview", rep)
        s8_sdk_writes_regular_to_workdir(client, "agent-with-preview", rep)
        s9_session_metadata(client, "digitorn-react-sandbox", rep)
        s10_preview_proxy(client, rep)
        s11_workdir_clean(client, "agent-with-preview", rep)
    except Exception as exc:
        rep.add("FATAL", False, f"{type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()

    return rep.summary()


if __name__ == "__main__":
    sys.exit(main())

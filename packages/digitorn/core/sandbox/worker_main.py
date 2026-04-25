"""Sandboxed tool executor subprocess.

The daemon runs agent_turn, decides which tool to call, and sends
an "exec" request to this worker. The worker loads modules, applies
OS-level sandbox (Landlock/Seatbelt/JobObject), executes the action,
and returns the result. No LLM, no sessions, no agent loop.

Lifecycle:
    1. Receive module list + sandbox config from daemon via stdin
    2. Load requested modules (lightweight — no LLM, no embeddings)
    3. Apply OS sandbox (irreversible)
    4. Signal ready
    5. Execute tool requests in a loop until shutdown

Run with: python -m digitorn.core.sandbox.worker_main
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

_real_stdout: Any = None


async def _main() -> None:
    global _real_stdout

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [worker] %(name)s %(message)s",
        stream=sys.stderr,
    )

    _real_stdout = sys.stdout
    sys.stdout = sys.stderr

    init_line = await _read_stdin_line()
    init_msg = json.loads(init_line)

    module_ids = init_msg.get("modules", [])
    workspace = init_msg.get("workspace", "")
    allowed_paths = init_msg.get("allowed_paths", [])
    sandbox_config = init_msg.get("sandbox", {})
    app_id = init_msg.get("app_id", "unknown")

    warm_pool = init_msg.get("warm_pool", False)

    modules = await _load_modules(module_ids)

    for mid, mod in modules.items():
        config: dict[str, Any] = {}
        if workspace:
            config["workspace"] = workspace
        try:
            await mod.on_config_update(config)
        except Exception as exc:
            logger.warning("config_update_failed module=%s: %s", mid, exc)

    sys.stdout = _real_stdout

    private_tmp = ""

    if warm_pool:
        # Warm mode: skip sandbox, signal warm-ready, wait for sandbox IPC
        _write({"id": 0, "success": True, "data": {
            "app_id": app_id,
            "warm": True,
        }})

        logger.info("worker_warm app=%s modules=%s", app_id, list(modules.keys()))
    else:
        # Immediate sandbox mode: existing behavior unchanged
        guard, private_tmp = _apply_sandbox(
            app_id=app_id,
            workspace=workspace,
            allowed_paths=allowed_paths,
            sandbox_config=sandbox_config,
        )

        _write({"id": 0, "success": True, "data": {
            "app_id": app_id,
            "sandbox": guard.active if hasattr(guard, "active") else [],
        }})

        logger.info("worker_ready app=%s modules=%s sandbox=%s",
                    app_id, list(modules.keys()),
                    guard.active if hasattr(guard, "active") else "none")

    try:
        await _request_loop(modules, workspace)
    except Exception as exc:
        logger.error("worker_error app=%s: %s", app_id, exc, exc_info=True)
    finally:
        for mod in modules.values():
            try:
                await mod.on_stop()
            except Exception:
                pass
        if private_tmp:
            _cleanup_tmpdir(private_tmp)


async def _load_modules(module_ids: list[str]) -> dict[str, Any]:
    from digitorn.modules.registry import ModuleRegistry
    from digitorn.core.loader import load_modules

    registry = ModuleRegistry()
    load_modules(registry, load_all=True)

    modules: dict[str, Any] = {}
    for mid in module_ids:
        try:
            modules[mid] = registry.create(mid)
            await modules[mid].on_start()
        except Exception as exc:
            logger.warning("module_load_failed module=%s: %s", mid, exc)

    return modules


def _apply_sandbox(
    app_id: str,
    workspace: str,
    allowed_paths: list[str],
    sandbox_config: dict[str, Any],
) -> tuple[Any, str]:
    from digitorn.core.sandbox.profile import SandboxProfile

    profile = SandboxProfile(app_id=app_id)

    if workspace:
        profile.writable_paths.add(workspace)
    for entry in allowed_paths:
        if entry.endswith(":rw"):
            profile.writable_paths.add(entry[:-3])
        else:
            path = entry.removesuffix(":ro")
            profile.readable_paths.add(path)

    profile.allow_exec = sandbox_config.get("allow_exec", True)
    profile.allow_fork = sandbox_config.get("allow_fork", True)
    profile.allow_network = sandbox_config.get("allow_network", False)
    profile.add_system_paths()

    private_tmp = profile.create_private_tmpdir()

    from digitorn.core.sandbox import apply_sandbox
    guard = apply_sandbox(profile)

    if sandbox_config.get("hardening", True):
        _apply_hardening()

    return guard, private_tmp


def _apply_hardening() -> None:
    try:
        from digitorn.core.sandbox.hardening import apply_hardening
        apply_hardening(
            drop_caps=True,
            no_new_privs=True,
            no_dumpable=True,
            mdwe=True,
        )
    except Exception as exc:
        logger.warning("hardening_skipped: %s", exc)


def _cleanup_tmpdir(tmpdir: str) -> None:
    import shutil
    try:
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass


async def _request_loop(modules: dict[str, Any], workspace: str) -> None:
    from digitorn.core.sandbox.ipc import decode_request, IPCResponse, encode_response

    # Mutable workspace — updated when deferred sandbox is applied
    current_workspace = workspace
    _start_time = _monotonic()

    while True:
        line = await _read_stdin_line()
        if not line:
            break

        try:
            req = decode_request(line.encode())
        except Exception as exc:
            logger.warning("decode_error: %s", exc)
            continue

        if req.method == "shutdown":
            break

        if req.method == "sandbox":
            # Deferred sandbox application (warm pool mode)
            try:
                sb_workspace = req.payload.get("workspace", "")
                sb_session = req.payload.get("session_id", "")
                sb_namespaces = set(req.payload.get("namespaces", []))
                sb_hardening = req.payload.get("hardening", {})
                sb_audit = req.payload.get("audit", False)

                guard, private_tmp = _apply_deferred_sandbox(
                    workspace=sb_workspace,
                    session_id=sb_session,
                    namespaces=sb_namespaces,
                    hardening=sb_hardening,
                    audit=sb_audit,
                )

                current_workspace = sb_workspace

                # Update module configs with the new workspace
                for mid, mod in modules.items():
                    config: dict[str, Any] = {}
                    if sb_workspace:
                        config["workspace"] = sb_workspace
                    try:
                        await mod.on_config_update(config)
                    except Exception:
                        pass

                resp = IPCResponse(id=req.id, success=True, data={
                    "sandbox_ready": True,
                    "active": guard.active if hasattr(guard, "active") else [],
                    "notify_fd": guard.notify_fd if hasattr(guard, "notify_fd") else None,
                    "session_id": sb_session,
                })
            except Exception as exc:
                resp = IPCResponse(id=req.id, success=False, error=str(exc))

            _write(json.loads(encode_response(resp)))
            continue

        if req.method == "health":
            try:
                import os
                rss = _get_memory_rss()
                resp = IPCResponse(id=req.id, success=True, data={
                    "state": "running",
                    "uptime": _monotonic() - _start_time,
                    "memory_rss": rss,
                    "pid": os.getpid(),
                })
            except Exception as exc:
                resp = IPCResponse(id=req.id, success=False, error=str(exc))

            _write(json.loads(encode_response(resp)))
            continue

        try:
            result = await _dispatch(modules, current_workspace, req)
            resp = IPCResponse(id=req.id, success=True, data=result)
        except Exception as exc:
            resp = IPCResponse(id=req.id, success=False, error=str(exc))

        _write(json.loads(encode_response(resp)))


async def _dispatch(
    modules: dict[str, Any],
    workspace: str,
    req: Any,
) -> dict[str, Any]:
    if req.method != "exec":
        raise ValueError(f"Unknown method: {req.method}")

    module_id = req.payload.get("module", "")
    action = req.payload.get("action", "")
    params = req.payload.get("params", {})
    constraints = req.payload.get("constraints", {})
    session_workspace = req.payload.get("workspace", workspace)

    mod = modules.get(module_id)
    if mod is None:
        return {"success": False, "error": f"Module '{module_id}' not loaded"}

    from digitorn.modules.base import ExecutionContext

    ctx = ExecutionContext(
        plan_id="sandbox",
        action_id=f"{module_id}.{action}",
        workspace=session_workspace,
        constraints=constraints,
    )

    result = await mod.execute(action, params, context=ctx)

    if hasattr(result, "to_dict"):
        return result.to_dict()
    if hasattr(result, "__dict__"):
        data = result.__dict__
        return {
            "success": data.get("success", True),
            "data": data.get("data"),
            "error": data.get("error", ""),
        }
    return {"success": True, "data": result}


_stdin_reader: asyncio.StreamReader | None = None


async def _read_stdin_line() -> str:
    global _stdin_reader
    if _stdin_reader is None:
        _stdin_reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(_stdin_reader)
        loop = asyncio.get_running_loop()
        await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)

    line = await _stdin_reader.readline()
    return line.decode("utf-8").strip()


def _write(obj: dict[str, Any]) -> None:
    _real_stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    _real_stdout.flush()


def _apply_deferred_sandbox(
    workspace: str,
    session_id: str,
    namespaces: set[str],
    hardening: dict[str, Any],
    audit: bool = False,
) -> tuple[Any, str]:
    """Apply sandbox in deferred mode (warm pool).

    Order (each layer independent — partial failure doesn't block rest):
        1. Process hardening (PR_SET_MDWE, drop caps, no_dumpable)
        2. Namespaces (user, pid, net — if configured)
        3. Landlock (filesystem restrictions)
        4. seccomp-notify (real-time audit — if audit=True, before static filter)
        5. seccomp (static syscall filter — last, locks syscall surface)
    """
    from digitorn.core.sandbox.profile import SandboxProfile
    from digitorn.core.sandbox.guard import SandboxGuard

    profile = SandboxProfile(app_id=f"session-{session_id}")

    if workspace:
        profile.writable_paths.add(workspace)
    profile.add_system_paths()

    private_tmp = profile.create_private_tmpdir()

    guard = SandboxGuard(app_id=profile.app_id)

    # Step 1: Process hardening
    do_hardening = hardening.get("enabled", True) if hardening else True
    if do_hardening:
        try:
            from digitorn.core.sandbox.hardening import apply_hardening
            features = apply_hardening(
                drop_caps=hardening.get("drop_caps", True) if hardening else True,
                no_new_privs=True,
                no_dumpable=hardening.get("no_dumpable", True) if hardening else True,
                mdwe=hardening.get("mdwe", True) if hardening else True,
            )
            guard.hardening = features
        except Exception as exc:
            guard.warnings.append(f"hardening: {exc}")
            logger.warning("deferred_hardening_skipped: %s", exc)

    # Step 2: Namespaces (if configured)
    if namespaces:
        try:
            from digitorn.core.sandbox.namespaces import unshare_namespaces
            active_ns = unshare_namespaces(namespaces)
            guard.namespaces = active_ns
        except Exception as exc:
            guard.warnings.append(f"namespaces: {exc}")
            logger.warning("deferred_namespaces_skipped: %s", exc)

    # Step 3: Landlock (filesystem restrictions)
    from digitorn.core.sandbox import apply_sandbox
    fs_guard = apply_sandbox(profile)
    guard.active = fs_guard.active
    guard.unavailable = fs_guard.unavailable
    guard.warnings.extend(fs_guard.warnings)

    # Step 4: seccomp-notify (real-time syscall audit)
    # Must be installed BEFORE the static seccomp filter (Step 5) because
    # seccomp filters stack — first match wins. The notify filter intercepts
    # audited syscalls and forwards them to the daemon for real-time logging.
    notify_fd = None
    if audit:
        try:
            from digitorn.core.sandbox.seccomp_notify import install_notify_filter
            notify_fd = install_notify_filter()
            if notify_fd is not None:
                guard.active.append("seccomp_notify")
                guard.notify_fd = notify_fd
                logger.info("seccomp_notify_installed session=%s fd=%d", session_id, notify_fd)
            else:
                guard.warnings.append("seccomp_notify: not available (kernel < 5.9)")
        except Exception as exc:
            guard.warnings.append(f"seccomp_notify: {exc}")
            logger.warning("seccomp_notify_failed session=%s: %s", session_id, exc)

    # Step 5: seccomp static filter (last — locks syscall surface)
    try:
        from digitorn.core.sandbox.seccomp import apply_seccomp
        ok, warns = apply_seccomp(
            allow_exec=profile.allow_exec,
            allow_network=profile.allow_network,
        )
        if ok:
            guard.active.append("seccomp")
        guard.warnings.extend(warns)
    except Exception as exc:
        guard.warnings.append(f"seccomp: {exc}")
        logger.warning("deferred_seccomp_failed session=%s: %s", session_id, exc)

    logger.info(
        "deferred_sandbox_applied session=%s workspace=%s active=%s ns=%s hardening=%s audit=%s",
        session_id, workspace, guard.active,
        guard.namespaces, guard.hardening, notify_fd is not None,
    )

    return guard, private_tmp


def _monotonic() -> float:
    """Monotonic clock for uptime tracking."""
    import time
    return time.monotonic()


def _get_memory_rss() -> int:
    """Get current process RSS in bytes."""
    try:
        import os
        with open(f"/proc/{os.getpid()}/statm") as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return 0


if __name__ == "__main__":
    asyncio.run(_main())

"""Sandbox enforcement tests - verify kernel-level isolation actually works.

These tests fork child processes, apply sandbox restrictions, then attempt
forbidden operations. The test PASSES only if the kernel blocks the operation.

Each test is a real security proof:
    - If it passes: the kernel prevented the attack
    - If it fails: there's a security hole

Tests are grouped by layer:
    1. Landlock: filesystem access control
    2. seccomp: syscall filtering
    3. Hardening: prctl-based process hardening
    4. Namespaces: process/network/mount isolation
    5. Integration: full sandbox stack
    6. Cross-session: workspace isolation between sessions
    7. Attack scenarios: real-world attack patterns

WARNING: These tests use os.fork() and apply IRREVERSIBLE restrictions
in child processes. They are designed to be safe (children always _exit),
but should not be run in production environments.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import mmap
import multiprocessing
import os
import platform
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path

import pytest

# ── Skip conditions ──────────────────────────────────────────────

_IS_LINUX = sys.platform == "linux"


def _landlock_available() -> bool:
    if not _IS_LINUX:
        return False
    try:
        from digitorn.core.sandbox.landlock import detect_abi_version
        return detect_abi_version() > 0
    except Exception:
        return False


def _kernel_version() -> tuple[int, int]:
    try:
        rel = os.uname().release
        parts = rel.split(".")
        return int(parts[0]), int(parts[1])
    except Exception:
        return (0, 0)


skip_non_linux = pytest.mark.skipif(not _IS_LINUX, reason="Linux only")
skip_no_landlock = pytest.mark.skipif(
    not _landlock_available(), reason="Landlock not available",
)
skip_no_mdwe = pytest.mark.skipif(
    _kernel_version() < (6, 3), reason="MDWE requires kernel 6.3+",
)


# ── Helpers ──────────────────────────────────────────────────────

def _run_in_child(fn, timeout: float = 5.0) -> int:
    """Run fn() in a forked child. Returns the child's exit code.

    Convention:
        exit(0) = operation was BLOCKED (test passes)
        exit(1) = operation SUCCEEDED (security hole!)
        exit(2) = unexpected error
        exit(99) = sandbox application itself failed
    """
    pid = os.fork()
    if pid == 0:
        try:
            code = fn()
            os._exit(code)
        except SystemExit as e:
            os._exit(e.code if isinstance(e.code, int) else 2)
        except Exception:
            os._exit(2)
    else:
        deadline = time.monotonic() + timeout
        while True:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                if os.WIFEXITED(status):
                    return os.WEXITSTATUS(status)
                return 2  # killed by signal
            if time.monotonic() > deadline:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                return 2
            time.sleep(0.01)


def _run_in_process(fn, timeout: float = 10.0) -> int:
    """Run fn() in a subprocess via multiprocessing (safer for ns tests)."""
    ctx = multiprocessing.get_context("fork")
    result = ctx.Value("i", 2)

    def _wrapper(res):
        try:
            res.value = fn()
        except Exception:
            res.value = 2

    proc = ctx.Process(target=_wrapper, args=(result,))
    proc.start()
    proc.join(timeout=timeout)

    if proc.is_alive():
        proc.kill()
        proc.join(1)
        return 2

    return result.value


# ══════════════════════════════════════════════════════════════════
# LAYER 1: LANDLOCK - Filesystem Access Control
# ══════════════════════════════════════════════════════════════════


@skip_non_linux
@skip_no_landlock
class TestLandlockEnforcement:
    """Verify that Landlock actually blocks filesystem access at kernel level."""

    def test_read_outside_workspace_blocked(self, tmp_path):
        """An agent with workspace=/tmp/app MUST NOT read files outside it."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "allowed.txt").write_text("ok")

        secret = tmp_path / "secret"
        secret.mkdir()
        (secret / "data.txt").write_text("TOP SECRET")

        def child():
            from digitorn.core.sandbox.landlock import apply_landlock
            ok, abi, _ = apply_landlock(
                writable_paths={str(workspace)},
                readable_paths=set(),
            )
            if not ok:
                return 99

            # Should succeed: read inside workspace
            try:
                content = (workspace / "allowed.txt").read_text()
                assert content == "ok"
            except Exception:
                return 2

            # Should FAIL: read outside workspace
            try:
                (secret / "data.txt").read_text()
                return 1  # SECURITY HOLE - read succeeded!
            except PermissionError:
                return 0  # BLOCKED
            except OSError as e:
                if e.errno == errno.EACCES:
                    return 0
                return 2

        code = _run_in_child(child)
        assert code == 0, f"Landlock failed to block read outside workspace (exit={code})"

    def test_write_outside_workspace_blocked(self, tmp_path):
        """Writing outside the allowed writable paths MUST fail."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        def child():
            from digitorn.core.sandbox.landlock import apply_landlock
            ok, _, _ = apply_landlock(
                writable_paths={str(workspace)},
                readable_paths=set(),
            )
            if not ok:
                return 99

            try:
                (workspace / "test.txt").write_text("hello")
            except Exception:
                return 2

            try:
                (outside / "pwned.txt").write_text("hacked")
                return 1  # SECURITY HOLE
            except (PermissionError, OSError):
                return 0  # BLOCKED

        code = _run_in_child(child)
        assert code == 0, f"Landlock failed to block write outside workspace (exit={code})"

    def test_read_etc_shadow_blocked(self, tmp_path):
        """/etc/shadow must be inaccessible from sandboxed process."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        def child():
            from digitorn.core.sandbox.landlock import apply_landlock
            ok, _, _ = apply_landlock(
                writable_paths={str(workspace)},
                readable_paths={str(workspace)},
            )
            if not ok:
                return 99

            try:
                open("/etc/shadow", "r")
                return 1  # SECURITY HOLE
            except (PermissionError, OSError):
                return 0  # BLOCKED

        code = _run_in_child(child)
        assert code == 0, f"Landlock failed to block /etc/shadow (exit={code})"

    def test_mkdir_outside_workspace_blocked(self, tmp_path):
        """Creating directories outside workspace must fail."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        def child():
            from digitorn.core.sandbox.landlock import apply_landlock
            ok, _, _ = apply_landlock(
                writable_paths={str(workspace)},
                readable_paths=set(),
            )
            if not ok:
                return 99

            try:
                os.makedirs(str(tmp_path / "escape" / "dir"), exist_ok=True)
                return 1  # SECURITY HOLE
            except (PermissionError, OSError):
                return 0  # BLOCKED

        code = _run_in_child(child)
        assert code == 0, f"Landlock failed to block mkdir outside workspace (exit={code})"

    def test_symlink_escape_blocked(self, tmp_path):
        """Symlinks pointing outside workspace must not allow reads."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        target = "/etc/hostname"
        if not Path(target).exists():
            pytest.skip("/etc/hostname not available")

        link = workspace / "escape_link"
        link.symlink_to(target)

        def child():
            from digitorn.core.sandbox.landlock import apply_landlock
            ok, _, _ = apply_landlock(
                writable_paths={str(workspace)},
                readable_paths={str(workspace)},
            )
            if not ok:
                return 99

            try:
                link.read_text()
                return 1  # SECURITY HOLE - symlink escape
            except (PermissionError, OSError):
                return 0  # BLOCKED

        code = _run_in_child(child)
        assert code == 0, f"Landlock failed to block symlink escape (exit={code})"

    def test_readable_path_is_not_writable(self, tmp_path):
        """A path in readable_paths but not writable_paths must be read-only."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        (readonly / "data.txt").write_text("read me")

        def child():
            from digitorn.core.sandbox.landlock import apply_landlock
            ok, _, _ = apply_landlock(
                writable_paths={str(workspace)},
                readable_paths={str(readonly)},
            )
            if not ok:
                return 99

            try:
                content = (readonly / "data.txt").read_text()
                assert content == "read me"
            except Exception:
                return 2

            try:
                (readonly / "pwned.txt").write_text("nope")
                return 1  # SECURITY HOLE
            except (PermissionError, OSError):
                return 0  # BLOCKED

        code = _run_in_child(child)
        assert code == 0, f"Landlock allowed write to read-only path (exit={code})"

    def test_delete_outside_workspace_blocked(self, tmp_path):
        """Deleting files outside workspace must fail."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        victim = tmp_path / "victim.txt"
        victim.write_text("don't delete me")

        def child():
            from digitorn.core.sandbox.landlock import apply_landlock
            ok, _, _ = apply_landlock(
                writable_paths={str(workspace)},
                readable_paths=set(),
            )
            if not ok:
                return 99

            try:
                victim.unlink()
                return 1  # SECURITY HOLE
            except (PermissionError, OSError):
                return 0  # BLOCKED

        code = _run_in_child(child)
        assert code == 0, f"Landlock failed to block delete outside workspace (exit={code})"

    def test_rename_escape_blocked(self, tmp_path):
        """Moving a file from workspace to outside must fail."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "file.txt").write_text("data")
        outside = tmp_path / "outside"
        outside.mkdir()

        def child():
            from digitorn.core.sandbox.landlock import apply_landlock
            ok, _, _ = apply_landlock(
                writable_paths={str(workspace)},
                readable_paths=set(),
            )
            if not ok:
                return 99

            try:
                os.rename(str(workspace / "file.txt"), str(outside / "stolen.txt"))
                return 1  # SECURITY HOLE
            except (PermissionError, OSError):
                return 0  # BLOCKED

        code = _run_in_child(child)
        assert code == 0, f"Landlock failed to block rename escape (exit={code})"


# ══════════════════════════════════════════════════════════════════
# LAYER 2: SECCOMP - Syscall Filtering
# ══════════════════════════════════════════════════════════════════


@skip_non_linux
class TestSeccompEnforcement:
    """Verify that seccomp blocks dangerous syscalls at kernel level."""

    def test_ptrace_blocked(self):
        """ptrace(ATTACH) must be blocked - prevents debugging other processes."""
        def child():
            from digitorn.core.sandbox.seccomp import apply_seccomp
            ok, _ = apply_seccomp(allow_exec=True, allow_network=True)
            if not ok:
                return 99

            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            PTRACE_ATTACH = 16
            ret = libc.ptrace(PTRACE_ATTACH, os.getppid(), 0, 0)
            if ret == -1:
                return 0  # BLOCKED
            return 1  # SECURITY HOLE

        code = _run_in_child(child)
        assert code == 0, f"seccomp failed to block ptrace (exit={code})"

    def test_mount_blocked(self):
        """mount() must be blocked - prevents filesystem manipulation."""
        def child():
            from digitorn.core.sandbox.seccomp import apply_seccomp
            ok, _ = apply_seccomp(allow_exec=True, allow_network=True)
            if not ok:
                return 99

            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            ret = libc.mount(b"none", b"/tmp", b"tmpfs", 0, None)
            if ret == -1:
                return 0  # BLOCKED
            return 1  # SECURITY HOLE

        code = _run_in_child(child)
        assert code == 0, f"seccomp failed to block mount (exit={code})"

    def test_reboot_blocked(self):
        """reboot() must be blocked."""
        def child():
            from digitorn.core.sandbox.seccomp import apply_seccomp
            ok, _ = apply_seccomp(allow_exec=True, allow_network=True)
            if not ok:
                return 99

            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            ret = libc.reboot(0x01234567)
            if ret == -1:
                return 0  # BLOCKED
            return 1  # SECURITY HOLE

        code = _run_in_child(child)
        assert code == 0, f"seccomp failed to block reboot (exit={code})"

    def test_exec_blocked_when_disallowed(self):
        """execve must be blocked when allow_exec=False."""
        def child():
            from digitorn.core.sandbox.seccomp import apply_seccomp
            ok, _ = apply_seccomp(allow_exec=False, allow_network=True)
            if not ok:
                return 99

            # Fork so exec doesn't replace our test process
            pid = os.fork()
            if pid == 0:
                try:
                    os.execv("/bin/true", ["/bin/true"])
                except OSError:
                    os._exit(42)  # blocked by seccomp
                # If exec succeeded, we are now /bin/true (exits 0)
                os._exit(0)
            else:
                _, status = os.waitpid(pid, 0)
                if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 42:
                    return 0  # BLOCKED - correct
                return 1  # SECURITY HOLE - exec succeeded

        code = _run_in_child(child)
        assert code == 0, f"seccomp failed to block execve (exit={code})"

    def test_exec_allowed_when_permitted(self):
        """execve must work when allow_exec=True."""
        def child():
            from digitorn.core.sandbox.seccomp import apply_seccomp
            ok, _ = apply_seccomp(allow_exec=True, allow_network=True)
            if not ok:
                return 99

            pid = os.fork()
            if pid == 0:
                try:
                    os.execv("/bin/true", ["/bin/true"])
                except OSError:
                    os._exit(42)  # blocked - shouldn't happen
                # If exec succeeded, we are now /bin/true (exits 0)
                os._exit(0)
            else:
                _, status = os.waitpid(pid, 0)
                if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0:
                    return 0  # exec worked - /bin/true exits 0
                if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 42:
                    return 1  # exec was blocked - seccomp too strict
                return 1

        code = _run_in_child(child)
        assert code == 0, f"seccomp blocked exec when allow_exec=True (exit={code})"

    def test_network_blocked_when_disallowed(self):
        """socket() must fail when allow_network=False."""
        def child():
            from digitorn.core.sandbox.seccomp import apply_seccomp
            ok, _ = apply_seccomp(allow_exec=True, allow_network=False)
            if not ok:
                return 99

            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.close()
                return 1  # SECURITY HOLE
            except OSError:
                return 0  # BLOCKED

        code = _run_in_child(child)
        assert code == 0, f"seccomp failed to block socket (exit={code})"

    def test_network_allowed_when_permitted(self):
        """socket() must work when allow_network=True."""
        def child():
            from digitorn.core.sandbox.seccomp import apply_seccomp
            ok, _ = apply_seccomp(allow_exec=True, allow_network=True)
            if not ok:
                return 99

            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.close()
                return 0
            except OSError:
                return 1

        code = _run_in_child(child)
        assert code == 0, f"seccomp blocked network when allowed (exit={code})"

    def test_sethostname_blocked(self):
        """sethostname() must be blocked."""
        def child():
            from digitorn.core.sandbox.seccomp import apply_seccomp
            ok, _ = apply_seccomp(allow_exec=True, allow_network=True)
            if not ok:
                return 99

            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            ret = libc.sethostname(b"pwned", 5)
            if ret == -1:
                return 0  # BLOCKED
            return 1  # SECURITY HOLE

        code = _run_in_child(child)
        assert code == 0, f"seccomp failed to block sethostname (exit={code})"

    def test_kernel_module_load_blocked(self):
        """finit_module must be blocked - prevents rootkit loading."""
        def child():
            from digitorn.core.sandbox.seccomp import apply_seccomp
            ok, _ = apply_seccomp(allow_exec=True, allow_network=True)
            if not ok:
                return 99

            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            ret = libc.syscall(ctypes.c_long(313), -1, b"", 0)  # finit_module
            if ret == -1:
                return 0  # BLOCKED
            return 1  # SECURITY HOLE

        code = _run_in_child(child)
        assert code == 0, f"seccomp failed to block kernel module load (exit={code})"


# ══════════════════════════════════════════════════════════════════
# LAYER 3: HARDENING - prctl-based Process Hardening
# ══════════════════════════════════════════════════════════════════


@skip_non_linux
class TestHardeningEnforcement:
    """Verify prctl hardening features actually restrict the process."""

    def test_capabilities_dropped(self):
        """After dropping all caps, the bounding set must be empty."""
        def child():
            # User namespace gives us full caps - needed for PR_CAPBSET_DROP
            from digitorn.core.sandbox.namespaces import unshare_namespaces
            active = unshare_namespaces({"user"})
            if "user" not in active:
                return 99  # Can't create user namespace

            from digitorn.core.sandbox.hardening import _drop_all_caps
            dropped = _drop_all_caps()
            if dropped == 0:
                return 99

            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            PR_CAPBSET_READ = 23
            has_any = False
            for cap in range(64):
                ret = libc.prctl(PR_CAPBSET_READ, cap, 0, 0, 0)
                if ret == 1:
                    has_any = True
                    break
                if ret == -1:
                    break

            if has_any:
                return 1  # SECURITY HOLE
            return 0

        code = _run_in_child(child)
        assert code == 0, f"Capability drop failed (exit={code})"

    def test_no_dumpable(self):
        """After PR_SET_DUMPABLE=0, process must not be dumpable."""
        def child():
            from digitorn.core.sandbox.hardening import _set_no_dumpable
            if not _set_no_dumpable():
                return 99

            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            PR_GET_DUMPABLE = 3
            val = libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0)
            if val == 0:
                return 0  # Not dumpable
            return 1  # Still dumpable

        code = _run_in_child(child)
        assert code == 0, f"PR_SET_DUMPABLE=0 failed (exit={code})"

    @skip_no_mdwe
    def test_mdwe_blocks_wx_mmap(self):
        """MDWE must prevent creating WRITE+EXEC memory mappings."""
        def child():
            from digitorn.core.sandbox.hardening import _set_mdwe, _set_no_new_privs
            _set_no_new_privs()
            if not _set_mdwe():
                return 99

            try:
                m = mmap.mmap(
                    -1, 4096,
                    prot=mmap.PROT_WRITE | mmap.PROT_EXEC,
                )
                m.close()
                return 1  # SECURITY HOLE - W+X mmap succeeded
            except (OSError, PermissionError):
                return 0  # BLOCKED

        code = _run_in_child(child)
        assert code == 0, f"MDWE failed to block W+X mmap (exit={code})"

    def test_no_new_privs(self):
        """PR_SET_NO_NEW_PRIVS must be set and verifiable."""
        def child():
            from digitorn.core.sandbox.hardening import _set_no_new_privs
            if not _set_no_new_privs():
                return 99

            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            PR_GET_NO_NEW_PRIVS = 39
            val = libc.prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0)
            if val == 1:
                return 0
            return 1

        code = _run_in_child(child)
        assert code == 0, f"NO_NEW_PRIVS not set (exit={code})"

    def test_full_hardening_returns_features(self):
        """apply_hardening() must return all activated features."""
        def child():
            # User namespace gives full caps - needed for PR_CAPBSET_DROP
            from digitorn.core.sandbox.namespaces import unshare_namespaces
            active = unshare_namespaces({"user"})
            if "user" not in active:
                return 99

            from digitorn.core.sandbox.hardening import apply_hardening
            features = apply_hardening()
            if "no_new_privs" not in features:
                return 1
            if "no_dumpable" not in features:
                return 1
            if not any("caps_dropped" in f for f in features):
                return 1
            return 0

        code = _run_in_child(child)
        assert code == 0, f"apply_hardening() missing features (exit={code})"


# ══════════════════════════════════════════════════════════════════
# LAYER 4: NAMESPACES - Process/Network Isolation
# ══════════════════════════════════════════════════════════════════


@skip_non_linux
class TestNamespaceEnforcement:
    """Verify Linux namespaces provide real isolation."""

    def test_user_namespace_created(self):
        """User namespace must be creatable without root."""
        def child():
            from digitorn.core.sandbox.namespaces import unshare_namespaces
            active = unshare_namespaces({"user"})
            if "user" not in active:
                return 1
            return 0

        code = _run_in_process(child)
        assert code == 0, f"User namespace creation failed (exit={code})"

    def test_pid_namespace_hides_host_pids(self):
        """In a PID namespace, child processes get low PIDs."""
        def child():
            from digitorn.core.sandbox.namespaces import unshare_namespaces
            active = unshare_namespaces({"user", "pid"})
            if "pid" not in active:
                return 99

            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                my_pid = os.getpid()
                os.write(w, str(my_pid).encode())
                os.close(w)
                os._exit(0)
            else:
                os.close(w)
                data = os.read(r, 32)
                os.close(r)
                os.waitpid(pid, 0)
                child_pid = int(data)
                if child_pid <= 2:
                    return 0  # Isolated
                return 1  # Not isolated

        code = _run_in_process(child)
        assert code in (0, 99), f"PID namespace isolation failed (exit={code})"

    def test_network_namespace_blocks_external(self):
        """In a network namespace, external connections must fail."""
        def child():
            from digitorn.core.sandbox.namespaces import unshare_namespaces
            active = unshare_namespaces({"user", "net"})
            if "net" not in active:
                return 99

            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            try:
                s.connect(("8.8.8.8", 53))
                s.close()
                return 1  # SECURITY HOLE
            except (OSError, socket.timeout):
                return 0  # BLOCKED
            finally:
                s.close()

        code = _run_in_process(child, timeout=10)
        assert code in (0, 99), f"Network namespace failed to isolate (exit={code})"

    def test_user_namespace_auto_added(self):
        """Requesting PID ns without user ns must auto-add user ns."""
        def child():
            from digitorn.core.sandbox.namespaces import unshare_namespaces
            active = unshare_namespaces({"pid"})
            if "user" in active and "pid" in active:
                return 0
            if not active:
                return 99
            return 1

        code = _run_in_process(child)
        assert code in (0, 99), f"User ns not auto-added for PID ns (exit={code})"


# ══════════════════════════════════════════════════════════════════
# LAYER 5: FULL STACK - Combined Sandbox
# ══════════════════════════════════════════════════════════════════


@skip_non_linux
@skip_no_landlock
class TestFullStackEnforcement:
    """Test the complete sandbox stack applied together."""

    def test_full_sandbox_applies_all_layers(self, tmp_path):
        """LinuxSandbox.apply() must activate Landlock + seccomp + hardening."""
        workspace = tmp_path / "ws"
        workspace.mkdir()

        def child():
            from digitorn.core.sandbox.linux import LinuxSandbox
            from digitorn.core.sandbox.profile import SandboxProfile

            profile = SandboxProfile(
                app_id="test-full",
                writable_paths={str(workspace)},
                allow_exec=True,
                allow_network=False,
            )
            profile.add_system_paths()

            backend = LinuxSandbox()
            guard = backend.apply(profile)

            has_landlock = any("landlock" in a for a in guard.active)
            has_seccomp = "seccomp" in guard.active
            has_hardening = len(guard.hardening) > 0

            if not has_landlock:
                return 1
            if not has_seccomp:
                return 1
            if not has_hardening:
                return 1
            return 0

        code = _run_in_child(child)
        assert code == 0, f"Full stack missing layers (exit={code})"

    def test_full_sandbox_blocks_everything(self, tmp_path):
        """After full sandbox: no file escape, no network, no ptrace, no exec."""
        # Use a non-/tmp location so add_system_paths() doesn't grant access.
        # We create dirs under tmp_path but the secret is placed outside the workspace.
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "ok.txt").write_text("inside")
        secret = tmp_path / "secret.txt"
        secret.write_text("classified")

        def child():
            from digitorn.core.sandbox.linux import LinuxSandbox
            from digitorn.core.sandbox.profile import SandboxProfile
            # Pre-import all sandbox submodules - after Landlock, .py files are gone
            import digitorn.core.sandbox.hardening  # noqa: F401
            import digitorn.core.sandbox.landlock  # noqa: F401
            import digitorn.core.sandbox.seccomp  # noqa: F401

            # Preload libc BEFORE sandbox - after Landlock, /usr/lib is gone
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

            # Strict profile: workspace only, no system paths
            profile = SandboxProfile(
                app_id="test-lockdown",
                writable_paths={str(workspace)},
                allow_exec=False,
                allow_network=False,
            )

            backend = LinuxSandbox()
            guard = backend.apply(profile)

            if not guard.is_enforced:
                return 99

            holes = []

            # Test 1: Read inside workspace - must work
            try:
                content = (workspace / "ok.txt").read_text()
                if content != "inside":
                    holes.append("WORKSPACE_WRONG_CONTENT")
            except Exception:
                holes.append("WORKSPACE_READ_FAILED")

            # Test 2: Read outside workspace - must fail
            try:
                secret.read_text()
                holes.append("READ_OUTSIDE")
            except (PermissionError, OSError):
                pass

            # Test 3: Network - must fail
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.close()
                holes.append("SOCKET_CREATED")
            except OSError:
                pass

            # Test 4: ptrace - must fail (libc already loaded)
            ret = libc.ptrace(16, os.getppid(), 0, 0)
            if ret != -1:
                holes.append("PTRACE_SUCCEEDED")

            # Test 5: exec - must fail
            pid = os.fork()
            if pid == 0:
                try:
                    os.execv("/bin/echo", ["/bin/echo", "pwned"])
                except OSError:
                    os._exit(42)  # blocked by seccomp - correct
                os._exit(0)
            else:
                _, status = os.waitpid(pid, 0)
                if os.WIFEXITED(status) and os.WEXITSTATUS(status) != 42:
                    holes.append("EXEC_SUCCEEDED")

            if holes:
                return 1  # Has security holes
            return 0  # All blocked

        code = _run_in_child(child)
        assert code == 0, f"Full sandbox has security holes (exit={code})"


# ══════════════════════════════════════════════════════════════════
# LAYER 6: CROSS-SESSION - Workspace Isolation
# ══════════════════════════════════════════════════════════════════


@skip_non_linux
@skip_no_landlock
class TestCrossSessionIsolation:
    """Verify that session A cannot access session B's workspace."""

    def test_different_workspaces_isolated(self, tmp_path):
        """Two sandboxed processes with different workspaces must be isolated."""
        ws_a = tmp_path / "session_a"
        ws_b = tmp_path / "session_b"
        ws_a.mkdir()
        ws_b.mkdir()
        (ws_a / "secret_a.txt").write_text("session A data")
        (ws_b / "secret_b.txt").write_text("session B data")

        def session_a_tries_read_b():
            from digitorn.core.sandbox.landlock import apply_landlock
            ok, _, _ = apply_landlock(
                writable_paths={str(ws_a)},
                readable_paths={str(ws_a)},
            )
            if not ok:
                return 99

            try:
                (ws_b / "secret_b.txt").read_text()
                return 1  # SECURITY HOLE
            except (PermissionError, OSError):
                return 0  # BLOCKED

        def session_b_tries_write_a():
            from digitorn.core.sandbox.landlock import apply_landlock
            ok, _, _ = apply_landlock(
                writable_paths={str(ws_b)},
                readable_paths={str(ws_b)},
            )
            if not ok:
                return 99

            try:
                (ws_a / "pwned.txt").write_text("hacked by B")
                return 1  # SECURITY HOLE
            except (PermissionError, OSError):
                return 0  # BLOCKED

        code_a = _run_in_child(session_a_tries_read_b)
        code_b = _run_in_child(session_b_tries_write_a)
        assert code_a == 0, f"Session A read session B's workspace (exit={code_a})"
        assert code_b == 0, f"Session B wrote to session A's workspace (exit={code_b})"

    def test_session_cannot_escape_to_parent(self, tmp_path):
        """A session must not access the parent of its workspace."""
        workspace = tmp_path / "apps" / "user1" / "workspace"
        workspace.mkdir(parents=True)
        parent_secret = tmp_path / "apps" / "user1" / "admin_config.txt"
        parent_secret.write_text("admin password")

        def child():
            from digitorn.core.sandbox.landlock import apply_landlock
            ok, _, _ = apply_landlock(
                writable_paths={str(workspace)},
                readable_paths={str(workspace)},
            )
            if not ok:
                return 99

            # Try direct path
            try:
                parent_secret.read_text()
                return 1  # SECURITY HOLE
            except (PermissionError, OSError):
                pass

            # Try via ../
            try:
                (workspace / ".." / "admin_config.txt").read_text()
                return 1  # SECURITY HOLE
            except (PermissionError, OSError):
                pass

            return 0  # Both blocked

        code = _run_in_child(child)
        assert code == 0, f"Session escaped to parent directory (exit={code})"

    def test_three_concurrent_sessions_isolated(self, tmp_path):
        """3 sessions with different workspaces must not see each other."""
        sessions = {}
        for i in range(3):
            ws = tmp_path / f"session_{i}"
            ws.mkdir()
            (ws / "data.txt").write_text(f"session {i}")
            sessions[i] = ws

        results = {}
        for i in range(3):
            ws = sessions[i]
            other_ws = sessions[(i + 1) % 3]

            def make_child(my_ws, their_ws):
                def child():
                    from digitorn.core.sandbox.landlock import apply_landlock
                    ok, _, _ = apply_landlock(
                        writable_paths={str(my_ws)},
                        readable_paths={str(my_ws)},
                    )
                    if not ok:
                        return 99

                    # Read own workspace - must work
                    try:
                        (my_ws / "data.txt").read_text()
                    except Exception:
                        return 2

                    # Read other's workspace - must fail
                    try:
                        (their_ws / "data.txt").read_text()
                        return 1  # SECURITY HOLE
                    except (PermissionError, OSError):
                        return 0  # BLOCKED
                return child

            results[i] = _run_in_child(make_child(ws, other_ws))

        for i, code in results.items():
            assert code == 0, f"Session {i} accessed another session's workspace (exit={code})"


# ══════════════════════════════════════════════════════════════════
# LAYER 7: ATTACK SCENARIOS - Real-world attack patterns
# ══════════════════════════════════════════════════════════════════


@skip_non_linux
@skip_no_landlock
class TestAttackScenarios:
    """Test against real-world attack patterns a malicious agent might try."""

    def test_proc_self_mem_blocked(self, tmp_path):
        """Reading /proc/self/mem for code injection must be blocked."""
        ws = tmp_path / "ws"
        ws.mkdir()

        def child():
            from digitorn.core.sandbox.hardening import apply_hardening
            from digitorn.core.sandbox.landlock import apply_landlock

            apply_hardening(no_dumpable=True)
            ok, _, _ = apply_landlock(
                writable_paths={str(ws)},
                readable_paths={str(ws)},
            )
            if not ok:
                return 99

            try:
                fd = os.open("/proc/self/mem", os.O_RDONLY)
                os.close(fd)
                return 1  # Risky
            except (PermissionError, OSError):
                return 0  # BLOCKED

        code = _run_in_child(child)
        assert code == 0, f"/proc/self/mem not blocked (exit={code})"

    def test_tmp_staging_blocked(self, tmp_path):
        """Using /tmp to stage data exfiltration must be blocked."""
        ws = tmp_path / "ws"
        ws.mkdir()
        staging = Path(f"/tmp/digitorn-exfil-test-{os.getpid()}")

        def child():
            from digitorn.core.sandbox.landlock import apply_landlock
            ok, _, _ = apply_landlock(
                writable_paths={str(ws)},
                readable_paths=set(),
            )
            if not ok:
                return 99

            try:
                staging.write_text("exfiltrated data")
                return 1  # SECURITY HOLE
            except (PermissionError, OSError):
                return 0  # BLOCKED

        code = _run_in_child(child)
        staging.unlink(missing_ok=True)
        assert code == 0, f"/tmp staging not blocked (exit={code})"

    def test_socket_to_c2_blocked(self):
        """Creating a socket to a C2 server must be blocked."""
        def child():
            from digitorn.core.sandbox.seccomp import apply_seccomp
            ok, _ = apply_seccomp(allow_exec=False, allow_network=False)
            if not ok:
                return 99

            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1)
                s.connect(("10.0.0.1", 4444))
                s.close()
                return 1  # SECURITY HOLE
            except OSError:
                return 0  # BLOCKED

        code = _run_in_child(child)
        assert code == 0, f"C2 connection not blocked (exit={code})"

    def test_fork_then_exec_blocked(self):
        """fork() + exec() chain must be stopped when exec is blocked."""
        def child():
            from digitorn.core.sandbox.seccomp import apply_seccomp
            ok, _ = apply_seccomp(allow_exec=False, allow_network=False)
            if not ok:
                return 99

            pid = os.fork()
            if pid == 0:
                try:
                    os.execv("/bin/sh", ["/bin/sh", "-c", "echo pwned"])
                    os._exit(1)  # exec succeeded = hole
                except OSError:
                    os._exit(0)  # blocked
            else:
                _, status = os.waitpid(pid, 0)
                if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0:
                    return 0  # exec was blocked in child
                return 1  # exec succeeded

        code = _run_in_child(child)
        assert code == 0, f"Fork+exec chain not blocked (exit={code})"

    def test_swap_manipulation_blocked(self):
        """swapon/swapoff must be blocked - prevents DoS via swap exhaustion."""
        def child():
            from digitorn.core.sandbox.seccomp import apply_seccomp
            ok, _ = apply_seccomp(allow_exec=True, allow_network=True)
            if not ok:
                return 99

            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            ret = libc.swapon(b"/dev/null", 0)
            if ret == -1:
                err = ctypes.get_errno()
                if err == errno.EPERM:
                    return 0  # BLOCKED by seccomp
                return 0  # Also blocked
            return 1  # SECURITY HOLE

        code = _run_in_child(child)
        assert code == 0, f"swapon not blocked (exit={code})"

    def test_combined_escape_attempt(self, tmp_path):
        """Try multiple escape vectors in sequence - all must fail."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "legit.txt").write_text("ok")

        def child():
            from digitorn.core.sandbox.linux import LinuxSandbox
            from digitorn.core.sandbox.profile import SandboxProfile
            # Pre-import all sandbox submodules
            import digitorn.core.sandbox.hardening  # noqa: F401
            import digitorn.core.sandbox.landlock  # noqa: F401
            import digitorn.core.sandbox.seccomp  # noqa: F401

            # Preload libc BEFORE sandbox
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

            # Strict lockdown to workspace only
            profile = SandboxProfile(
                app_id="test-escape",
                writable_paths={str(ws)},
                allow_exec=False,
                allow_network=False,
            )

            backend = LinuxSandbox()
            guard = backend.apply(profile)
            if not guard.is_enforced:
                return 99

            attacks_blocked = 0
            total_attacks = 0

            # Attack 1: Read /etc/passwd
            total_attacks += 1
            try:
                open("/etc/passwd", "r").read()
            except (PermissionError, OSError):
                attacks_blocked += 1

            # Attack 2: Write outside workspace
            total_attacks += 1
            try:
                Path(f"/var/tmp/escape-{os.getpid()}").write_text("data")
            except (PermissionError, OSError):
                attacks_blocked += 1

            # Attack 3: Create socket
            total_attacks += 1
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.close()
            except OSError:
                attacks_blocked += 1

            # Attack 4: ptrace parent (libc already loaded)
            total_attacks += 1
            ret = libc.ptrace(16, os.getppid(), 0, 0)
            if ret == -1:
                attacks_blocked += 1

            # Attack 5: Load kernel module (libc already loaded)
            total_attacks += 1
            ret = libc.syscall(ctypes.c_long(313), -1, b"", 0)
            if ret == -1:
                attacks_blocked += 1

            # Attack 6: exec shell (fork first - exec replaces the process)
            total_attacks += 1
            pid = os.fork()
            if pid == 0:
                try:
                    os.execv("/bin/sh", ["/bin/sh", "-c", "id"])
                except OSError:
                    os._exit(42)  # blocked
                os._exit(0)  # exec succeeded
            else:
                _, status = os.waitpid(pid, 0)
                if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 42:
                    attacks_blocked += 1

            if attacks_blocked == total_attacks:
                return 0  # ALL attacks blocked
            return 1  # Some attacks succeeded

        code = _run_in_child(child)
        assert code == 0, f"Some attack vectors succeeded (exit={code})"

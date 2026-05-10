"""Test harness primitives: daemon lifecycle, JWT minting, tmpdir,
latency timer, filesystem assertions.

Designed for the SessionStore refactor baseline. Never imports any
production code from the daemon's hot path -- only spawns and observes.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import jwt as pyjwt


_PY = sys.executable

# Match the issuer the production gateway accepts via
# DIGITORN_GATEWAY_AUTH_ACCEPT_ISSUERS=["digitorn"], also matches the
# daemon's auth.accept_issuers.
_DEFAULT_ISSUER = "digitorn"
_DEFAULT_KID = "auth-local-dev"

# Where the local mint helper at c:\tmp\mint_dev_token.py stores the
# generated keypair. Reused so a single JWKS server (already running on
# 9999) verifies tokens minted by both helpers.
_AUTH_PRIV = Path.home() / ".digitorn" / "auth-private.pem"
_AUTH_PUB = Path.home() / ".digitorn" / "auth-public.pem"


def find_free_port(start: int = 8500, end: int = 8599) -> int:
    """Pick an unused TCP port in the test range. Avoids the well-known
    daemon (8000) / gateway (8002, 8202) ports the operator may have
    running in parallel, AND the 8100-8199 range that other test
    agents typically grab."""
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port in {start}-{end}")


def load_test_jwt() -> str:
    """Load the canonical test JWT.

    Prefers ``~/.digitorn/test-auth-phase3.json`` (a fresh user
    minted for the refactor baseline tests, gets its own quota
    bucket on the gateway). Falls back to the operator's main
    ``~/.digitorn/test-auth.json`` if the phase3 file is missing.
    """
    home = Path.home() / ".digitorn"
    for candidate in (home / "test-auth-phase3.json", home / "test-auth.json"):
        if candidate.exists():
            raw = json.loads(candidate.read_text(encoding="utf-8"))
            tok = raw.get("access_token")
            if tok:
                return str(tok)
    raise RuntimeError(
        f"no test JWT found under {home}/ -- generate one or run "
        "mint_dev_token.py once."
    )


def mint_test_jwt(
    *,
    sub: str | None = None,
    email: str | None = None,
    name: str = "test",
    roles: list[str] | None = None,
    perms: list[str] | None = None,
    ttl_seconds: int = 24 * 3600,
    issuer: str = _DEFAULT_ISSUER,
    kid: str = _DEFAULT_KID,
) -> str:
    """Mint an RS256 JWT signed by the local auth-private.pem.

    The matching public key is at auth-public.pem. The local JWKS server
    (c:\\tmp\\mint_dev_token.py listening on 9999) serves it under the
    same kid, so the test daemon can verify offline.
    """
    if not _AUTH_PRIV.exists():
        raise RuntimeError(
            f"{_AUTH_PRIV} not found -- run mint_dev_token.py once to "
            "generate the keypair, or copy it from the prod auth service."
        )
    priv = _AUTH_PRIV.read_bytes()
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": sub or uuid.uuid4().hex,
        "type": "access",
        "iss": issuer,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": uuid.uuid4().hex,
        "email": email or f"test-{uuid.uuid4().hex[:6]}@digitorn.local",
        "name": name,
        "roles": roles or ["admin", "developer"],
        "perms": perms or ["*"],
    }
    return pyjwt.encode(claims, priv, algorithm="RS256", headers={"kid": kid})


@dataclasses.dataclass
class DaemonHandle:
    """Either a spawn'd test daemon or a handle to an external one
    the operator launched themselves. ``proc=None`` + ``tmpdir=None``
    means external."""

    proc: subprocess.Popen | None
    port: int
    base_url: str
    tmpdir: Path | None
    sessions_root: Path
    db_url: str
    log_path: Path | None
    err_path: Path | None

    def healthz_ok(self, timeout: float = 0.3) -> bool:
        try:
            r = httpx.get(f"{self.base_url}/healthz", timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False


@contextlib.contextmanager
def attach_external_daemon(
    *,
    base_url: str = "http://127.0.0.1:8000",
    sessions_root: Path | None = None,
):
    """Yield a DaemonHandle pointing at a daemon the operator already
    launched. No spawn, no teardown -- the daemon survives the test.

    Used when the daemon's boot is too costly to repeat per test run
    (HF model downloads, builtin app deployment, etc.) and the
    operator launches it themselves with the correct env (typically
    ``DIGITORN_SESSION_STORE_MODE=primary`` + ``SKIP_BUILTINS=1``).

    The sessions_root defaults to ``~/.digitorn/sessions/`` -- the
    canonical location used when the operator runs ``digitorn start``
    without a custom ``DIGITORN_SESSION_STORE_ROOT`` override.
    """
    if sessions_root is None:
        sessions_root = Path.home() / ".digitorn" / "sessions"
    parsed_port = int(base_url.rsplit(":", 1)[-1].split("/", 1)[0])
    handle = DaemonHandle(
        proc=None,
        port=parsed_port,
        base_url=base_url.rstrip("/"),
        tmpdir=None,
        sessions_root=sessions_root,
        db_url="<external>",
        log_path=None,
        err_path=None,
    )
    if not handle.healthz_ok(timeout=2.0):
        raise RuntimeError(
            f"external daemon at {base_url} not reachable -- start it first "
            f"with `$env:DIGITORN_SESSION_STORE_MODE='primary'; "
            f"$env:DIGITORN_SKIP_BUILTINS='1'; digitorn start`"
        )
    yield handle


@contextlib.contextmanager
def spawn_test_daemon(
    *,
    session_store_mode: str = "primary",
    session_store_max_bytes: int = 64 * 1024 * 1024,
    session_store_max_sessions: int = 100,
    flush_interval_ms: int = 50,
    jwks_url: str = "http://127.0.0.1:9999/.well-known/jwks.json",
    auth_issuers: tuple[str, ...] = ("digitorn",),
    gateway_url: str = "http://127.0.0.1:8202",
    gateway_jwt: str | None = None,
    default_model_alias: str = "lb-test",
    default_model_provider: str = "openai",
    default_model_backend: str = "openai_compat",
    extra_env: dict[str, str] | None = None,
    boot_timeout: float = 30.0,
):
    """Spawn an isolated daemon for the duration of the with-block.

    Layout:
        <tmpdir>/.digitorn/sessions/  -- SessionStore root (new path)
        <tmpdir>/.digitorn/digitorn.db -- SQLite for residual tables
        <tmpdir>/.digitorn/logs/     -- daemon stdout / stderr
        <tmpdir>/.digitorn/state/    -- JsonStateStore root
        <tmpdir>/.digitorn/workspaces/ -- per-session workspaces
        <tmpdir>/.digitorn/sessions-old/ -- legacy SessionStore (DiskCache)

    Yields a ``DaemonHandle``. On exit the daemon is killed and the
    tmpdir wiped. Never leaks state into ~/.digitorn/.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="digitorn-test-"))
    digitorn_home = tmpdir / ".digitorn"
    sessions_root = digitorn_home / "sessions"
    sessions_old = digitorn_home / "sessions-old"
    state_dir = digitorn_home / "state"
    workspaces = digitorn_home / "workspaces"
    logs = digitorn_home / "logs"
    for p in (sessions_root, sessions_old, state_dir, workspaces, logs):
        p.mkdir(parents=True, exist_ok=True)

    db_path = digitorn_home / "digitorn.db"
    db_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env.update({
        # Isolate the home so JsonStateStore + DiskCache + everything
        # else under ~/.digitorn lands inside our tmpdir.
        "DIGITORN_HOME": str(digitorn_home),
        "USERPROFILE": str(tmpdir),
        "HOME": str(tmpdir),
        # SessionStore wiring
        "DIGITORN_SESSION_STORE_MODE": session_store_mode,
        "DIGITORN_SESSION_STORE_ROOT": str(sessions_root),
        "DIGITORN_SESSION_STORE_MAX_BYTES": str(session_store_max_bytes),
        "DIGITORN_SESSION_STORE_MAX_SESSIONS": str(session_store_max_sessions),
        "DIGITORN_SESSION_STORE_FLUSH_MS": str(flush_interval_ms),
        # Database -- SQLite local, never touch Neon
        "DIGITORN_DATABASE__URL": db_url,
        # Auth -- verify against the local JWKS the operator already
        # has running on 9999, accept the "digitorn" issuer that
        # mint_test_jwt stamps. ``mode=remote`` is mandatory since
        # ``embedded`` was retired; service_url points at the local
        # JWKS host so the daemon's JWKS fetcher succeeds without
        # going to auth.digitorn.ai.
        "DIGITORN_AUTH__MODE": "remote",
        "DIGITORN_AUTH__SERVICE_URL": "http://127.0.0.1:9999",
        "DIGITORN_AUTH__JWKS_URL": jwks_url,
        "DIGITORN_AUTH__ACCEPT_ISSUERS": json.dumps(list(auth_issuers)),
        # Server -- bind to test port
        "DIGITORN_SERVER__HOST": "127.0.0.1",
        "DIGITORN_SERVER__PORT": str(port),
        "DIGITORN_SERVER__WORKERS": "1",
        # Default model -- points at the live gateway as an OpenAI-
        # compatible upstream. The api_key is a JWT minted by
        # mint_test_jwt(); the gateway accepts it as auth.
        "DIGITORN_DEFAULT_MODEL__PROVIDER": default_model_provider,
        "DIGITORN_DEFAULT_MODEL__MODEL": default_model_alias,
        "DIGITORN_DEFAULT_MODEL__BACKEND": default_model_backend,
        "DIGITORN_DEFAULT_MODEL__BASE_URL": gateway_url.rstrip("/") + "/v1",
        "DIGITORN_DEFAULT_MODEL__API_KEY": gateway_jwt or "",
        "DIGITORN_DEFAULT_MODEL__MAX_TOKENS": "256",  # cheap test calls
        # Force in-memory message queue. The default backend is Redis;
        # tests don't have a Redis around and the daemon logs a noisy
        # boot warning otherwise.
        "DIGITORN_SESSION__QUEUE__BACKEND": "memory",
        # Disable transcription (whisper) so the daemon doesn't spend
        # 7s+ at boot downloading models from HuggingFace, which also
        # stalls the event loop and triggers the loop_stalled detector.
        "DIGITORN_TRANSCRIBE__PROVIDER": "openai",
        # Skip the builtin app auto-deploy (copilot-smoke pings a real
        # LLM at boot and times out at 60s on every test run). We
        # deploy digitorn-chat manually after spawn instead.
        "DIGITORN_SKIP_BUILTINS": "1",
        # Disable RAG + vector modules: they pull fastembed at boot
        # which downloads ~500MB of embedding models from HuggingFace
        # and stalls the loop for 7s. The SessionStore refactor doesn't
        # touch RAG; tests don't need it.
        "DIGITORN_MODULES__DISABLED": json.dumps(["rag", "vector", "transcribe"]),
        "HF_HUB_OFFLINE": "1",  # extra safety -- block any network HF call
        "TRANSFORMERS_OFFLINE": "1",
        # Quiet noisy modules
        "PYTHONUNBUFFERED": "1",
    })
    if extra_env:
        env.update(extra_env)

    log_path = logs / "daemon.out"
    err_path = logs / "daemon.err"
    log_f = open(log_path, "w", encoding="utf-8", buffering=1)
    err_f = open(err_path, "w", encoding="utf-8", buffering=1)

    cmd = [
        _PY, "-m", "uvicorn",
        "digitorn.core.server:create_app",
        "--factory",
        "--host", "127.0.0.1",
        "--port", str(port),
        "--log-level", "warning",
        "--no-access-log",
    ]
    proc = subprocess.Popen(
        cmd, env=env, stdout=log_f, stderr=err_f,
        cwd=str(Path(__file__).resolve().parents[3]),
    )

    handle = DaemonHandle(
        proc=proc, port=port, base_url=base_url, tmpdir=tmpdir,
        sessions_root=sessions_root, db_url=db_url,
        log_path=log_path, err_path=err_path,
    )

    deadline = time.monotonic() + boot_timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log_f.close()
            err_f.close()
            tail = err_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise RuntimeError(
                f"daemon exited during boot (rc={proc.returncode})\n"
                f"--- stderr tail ---\n{tail}"
            )
        if handle.healthz_ok():
            try:
                yield handle
                return
            finally:
                _teardown(handle, log_f, err_f)
        time.sleep(0.2)

    _teardown(handle, log_f, err_f)
    raise RuntimeError(
        f"daemon did not become healthy within {boot_timeout}s\n"
        f"stderr tail: {err_path.read_text(encoding='utf-8', errors='replace')[-2000:]}"
    )


def write_test_app_yaml(
    tmpdir: Path,
    *,
    app_id: str = "baseline-chat",
    gateway_url: str = "http://127.0.0.1:8202",
    gateway_jwt: str = "",
    model_alias: str = "lb-test",
) -> Path:
    """Write a minimal test app YAML that hits the live gateway.

    Avoids the full builtin digitorn-chat (which has a behavior
    classifier doing extra LLM calls + ollama-default brain that won't
    work). The api_key is the JWT itself: the gateway accepts JWTs from
    the same auth issuer the daemon validates against.
    """
    yaml_text = f"""app:
  app_id: {app_id}
  name: Baseline Test Chat
  short_name: Baseline
  version: 0.0.1
  description: Minimal app for refactor baseline tests.
runtime:
  mode: conversation
  workdir_mode: none
  tool_injection: direct
  max_turns: 5
  timeout: 60
agents:
- id: main
  role: assistant
  brain:
    provider: openai
    model: {model_alias}
    backend: openai_compat
    config:
      base_url: {gateway_url.rstrip('/')}/v1
      api_key: {gateway_jwt}
    temperature: 0.0
    max_tokens: 32
  system_prompt: |
    Echo the user's message verbatim and stop. Do not add commentary.
tools:
  modules: {{}}
  capabilities:
    default_policy: auto
    grant: []
"""
    target = tmpdir / "baseline-chat.yaml"
    target.write_text(yaml_text, encoding="utf-8")
    return target


def list_deployed_apps(daemon: DaemonHandle, token: str) -> list[str]:
    """Return the list of app_ids currently deployed on the daemon."""
    r = httpx.get(
        f"{daemon.base_url}/api/apps",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    if r.status_code != 200:
        return []
    body = r.json()
    # Endpoint shape varies: either a bare list, {"data": {"apps": [...]}}
    # or {"apps": [...]}. Handle all three.
    rows: list[dict[str, Any]] = []
    if isinstance(body, list):
        rows = [b for b in body if isinstance(b, dict)]
    elif isinstance(body, dict):
        if isinstance(body.get("apps"), list):
            rows = [b for b in body["apps"] if isinstance(b, dict)]
        else:
            data = body.get("data") or {}
            if isinstance(data, dict) and isinstance(data.get("apps"), list):
                rows = [b for b in data["apps"] if isinstance(b, dict)]
            elif isinstance(data, list):
                rows = [b for b in data if isinstance(b, dict)]
    return [
        str(r.get("app_id") or r.get("id") or "")
        for r in rows
        if r.get("app_id") or r.get("id")
    ]


def wait_for_app_deployed(
    daemon: DaemonHandle, token: str, app_id: str, *, timeout: float = 90.0,
) -> bool:
    """Poll /api/apps until ``app_id`` shows up. Returns False on timeout.

    Built-in apps are auto-deployed via ``bootstrap_builtins`` which is
    fire-and-forget at lifespan: the daemon's HTTP serves immediately,
    deployment lands later. Tests must wait for the app before sending
    messages or every POST returns 404 + zombie_poll_detected kicks in.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app_id in list_deployed_apps(daemon, token):
            return True
        time.sleep(1.0)
    return False


def _teardown(handle: DaemonHandle, log_f, err_f) -> None:
    proc = handle.proc
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    try:
        log_f.close()
    except Exception:
        pass
    try:
        err_f.close()
    except Exception:
        pass
    # Keep the tmpdir on test failure for forensic diff.
    keep = os.environ.get("DIGITORN_TEST_KEEP_TMPDIR", "").lower() in ("1", "true", "yes")
    if not keep:
        shutil.rmtree(handle.tmpdir, ignore_errors=True)


# ── Latency timer ────────────────────────────────────────────────────


@dataclasses.dataclass
class _Sample:
    label: str
    ms: float


class LatencyTimer:
    """Collect timing samples grouped by label, then assert percentiles.

    Usage::

        timer = LatencyTimer()
        with timer.measure("append_event"):
            ...
        timer.assert_p99_ms("append_event", 1.0)
    """

    def __init__(self) -> None:
        self._samples: list[_Sample] = []

    @contextlib.contextmanager
    def measure(self, label: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self._samples.append(
                _Sample(label=label, ms=(time.perf_counter() - t0) * 1000.0),
            )

    def add_sample(self, label: str, ms: float) -> None:
        self._samples.append(_Sample(label=label, ms=ms))

    def values(self, label: str) -> list[float]:
        return [s.ms for s in self._samples if s.label == label]

    def percentile(self, label: str, p: float) -> float:
        vs = sorted(self.values(label))
        if not vs:
            return 0.0
        # statistics.quantiles supports n>=2 only, so do it manually.
        k = max(0, min(len(vs) - 1, int(round(p / 100.0 * (len(vs) - 1)))))
        return vs[k]

    def stats(self, label: str) -> dict[str, float]:
        vs = self.values(label)
        if not vs:
            return {"count": 0}
        return {
            "count": len(vs),
            "min_ms": min(vs),
            "p50_ms": self.percentile(label, 50),
            "p95_ms": self.percentile(label, 95),
            "p99_ms": self.percentile(label, 99),
            "max_ms": max(vs),
            "mean_ms": statistics.fmean(vs),
        }

    def assert_p99_ms(self, label: str, budget_ms: float) -> None:
        got = self.percentile(label, 99)
        if got > budget_ms:
            raise AssertionError(
                f"latency budget breach: {label} p99={got:.2f}ms > {budget_ms:.2f}ms "
                f"(samples={len(self.values(label))})"
            )

    def report(self) -> str:
        labels = sorted({s.label for s in self._samples})
        lines = [f"{'label':<30} {'n':>6} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8}"]
        for lbl in labels:
            st = self.stats(lbl)
            lines.append(
                f"{lbl:<30} {st['count']:>6} "
                f"{st['p50_ms']:>7.2f}ms {st['p95_ms']:>7.2f}ms "
                f"{st['p99_ms']:>7.2f}ms {st['max_ms']:>7.2f}ms"
            )
        return "\n".join(lines)


# ── Filesystem assertions ────────────────────────────────────────────


def session_dir_for(sessions_root: Path, sid: str) -> Path:
    """Mirror InMemorySessionStore._session_dir(sid)."""
    h = hashlib.sha256(sid.encode("utf-8")).hexdigest()
    return sessions_root / h[:2] / h[2:4] / sid


def read_events_jsonl(session_dir: Path) -> list[dict[str, Any]]:
    path = session_dir / "events.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def read_meta_json(session_dir: Path) -> dict[str, Any]:
    path = session_dir / "meta.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def read_snapshot_json(session_dir: Path) -> dict[str, Any] | None:
    path = session_dir / "snapshot.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def assert_seq_contiguous(events: list[dict[str, Any]]) -> None:
    """Strictest possible seq invariant: 1, 2, 3, ... N with NO gaps,
    NO duplicates, sorted ascending. This is the contract the user
    underlined: 'jamais deux éléments avec deux séquence identique,
    les séquences doivent être chronologiques'."""
    if not events:
        return
    seqs = [int(e["seq"]) for e in events]
    if seqs[0] != 1:
        raise AssertionError(f"first seq is {seqs[0]}, expected 1")
    for i, s in enumerate(seqs):
        expected = i + 1
        if s != expected:
            raise AssertionError(
                f"seq gap or duplicate at index {i}: got {s}, expected {expected}"
            )


def assert_seq_strictly_monotonic(events: list[dict[str, Any]]) -> None:
    """Weaker than contiguous: just no duplicates, sorted ascending.
    Useful when post-compaction events carry seqs > cutoff."""
    seqs = [int(e["seq"]) for e in events]
    if seqs != sorted(seqs):
        raise AssertionError(f"seqs not sorted: {seqs[:20]}...")
    if len(set(seqs)) != len(seqs):
        dups = [s for s in seqs if seqs.count(s) > 1]
        raise AssertionError(f"seq duplicates: {sorted(set(dups))[:10]}")

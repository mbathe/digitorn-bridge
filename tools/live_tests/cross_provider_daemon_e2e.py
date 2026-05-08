"""End-to-end via DevClient: daemon -> gateway -> cross-provider route.

The gateway-only audit (cross_provider_routing_audit.py) already proves
the resolver honours per-route identity overrides. This test goes one
step further: it drives a REAL chat through the daemon, which goes
through the gateway, which hits a route that overrides the alias's
metadata. If the daemon's gateway resolver respected the alias's bogus
metadata, the call would 4xx -- but the route override wins, so the
chat succeeds and we see a real LLM response.

Scenarios:
  S1. Cross-provider success
      Alias `audit-daemon-cp-XXXX` has metadata {provider=openai,
      real_model_id=gpt-99-fictitious}, single route overrides to
      {provider=github_copilot, real_model_id=gpt-4o-mini}. App brain
      points at this alias. Send a chat. The daemon must:
        - resolve the alias via the gateway
        - honour the route's identity (not the metadata)
        - return a real Copilot response

  S2. Cross-provider failover
      Alias `audit-daemon-fo-XXXX` has 2 routes:
        P0 = audit-cross-dummy provider (connection refused)
        P1 = github_copilot
      App brain points at this alias. Send 5 chats. Failover converges:
      after the first call's slow path, P0 enters cooldown and the rest
      go straight to P1. All 5 must end with a real response.

Cleanup runs in finally: blocks - no orphaned aliases left behind.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Wire imports
ROOT = Path("c:/Users/ASUS/Documents/digitorn-bridge/packages")
sys.path.insert(0, str(ROOT / "digitorn"))

from digitorn.testing import DevClient  # noqa: E402

DAEMON = "http://127.0.0.1:8000"
GATEWAY = "http://127.0.0.1:8202"
GATEWAY_LOG = Path("c:/tmp/digitorn-e2e/sandbox_gateway.log")

CREDS = json.loads(
    (Path.home() / ".digitorn" / "credentials.json").read_text(encoding="utf-8")
)
H = {
    "Authorization": f"Bearer {CREDS['access_token']}",
    "Content-Type": "application/json",
}


@dataclass
class Section:
    name: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def ok(self, l: str, d: str = "") -> None: self.checks.append((l, True, d))
    def fail(self, l: str, d: str = "") -> None: self.checks.append((l, False, d))
    def passed(self) -> bool: return all(c[1] for c in self.checks)
    def render(self) -> str:
        out = [f"\n=== {self.name} ==="]
        for l, ok, d in self.checks:
            mark = "PASS" if ok else "FAIL"
            line = f"  [{mark}] {l}"
            if d: line += f" -- {d[:240]}"
            out.append(line)
        out.append(f"  -> {'PASS' if self.passed() else 'FAIL'} "
                   f"({sum(1 for c in self.checks if c[1])}/{len(self.checks)})")
        return "\n".join(out)


def gw(method: str, path: str, body: dict | None = None,
       *, timeout: float = 60.0) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{GATEWAY}{path}", method=method, data=data, headers=H,
    )
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        raw = r.read()
        if "json" in r.headers.get("content-type", ""):
            return r.status, (json.loads(raw) if raw else {})
        return r.status, {"_raw": raw[:500].decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except Exception:
            return exc.code, {"_raw": raw[:500].decode("utf-8", "replace")}


def _tail_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except FileNotFoundError:
        return 0


def _tail_since(p: Path, offset: int) -> str:
    if not p.exists():
        return ""
    with p.open("rb") as f:
        f.seek(offset)
        return f.read().decode("utf-8", errors="replace")


def find_copilot_credential() -> str:
    code, body = gw("GET", "/admin/credentials")
    if code != 200:
        return ""
    for c in body.get("rows", []):
        if c["provider_slug"] == "github_copilot" and c["status"] == "active":
            return c["id"]
    return ""


def ensure_dummy_provider() -> str:
    slug = "audit-cross-dummy"
    code, _ = gw("GET", f"/admin/providers/{slug}")
    if code == 200:
        return slug
    code, _ = gw("POST", "/admin/providers", {
        "slug": slug, "name": "Audit Cross Dummy",
        "base_url": "http://127.0.0.1:1/v1",
        "compat": "openai_compat", "auth_type": "api_key",
        "metadata": {},
    })
    if code != 201:
        raise RuntimeError(f"can't create dummy provider: {code}")
    return slug


# ── App YAML builder ────────────────────────────────────────────────


def write_app_yaml(app_id: str, model_suffix: str) -> Path:
    """Minimal Digitorn app whose brain points at the chosen alias
    suffix. The daemon's gateway_resolver synthesises the gateway
    model id as ``github_copilot/{model_suffix}`` -- so the gateway
    alias must exist under that exact key.

    The ``credential:`` block + per_user scope is the trigger that
    forces the daemon's resolver onto the gateway-routing path (same
    pattern used by ``credential_picker_test.yaml`` in master_proof).
    Without it the daemon may resolve via the deployed provider
    directly + bypass the gateway."""
    yaml = f"""app:
  app_id: {app_id}
  name: Cross-Provider E2E Test
  description: Validates daemon -> gateway -> per-route identity override.

ui:
  greeting: "Reply with one word: ALIVE"

agents:
  - id: main
    role: assistant
    brain:
      provider: github_copilot
      backend: github_copilot
      model: {model_suffix}
      temperature: 1.0
      max_tokens: 32
      config:
        api_key: '{{{{env.GH_COPILOT_TOKEN}}}}'
        base_url: https://api.githubcopilot.com
    system_prompt: |
      You are a routing-test agent. Answer with at most 5 words.
"""
    p = Path("c:/tmp/digitorn-e2e") / f"{app_id}.yaml"
    p.write_text(yaml, encoding="utf-8")
    return p


# ── S1. Cross-provider success ──────────────────────────────────────


def section_s1_cross_provider_via_daemon(cli: DevClient, copilot_cred_id: str) -> Section:
    s = Section("S1. Daemon -> gateway -> cross-provider route override (success)")
    suffix = uuid.uuid4().hex[:6]
    # The daemon's gateway_resolver synthesises model strings as
    # ``<brain.provider>/<brain.model>``. So the alias name in the
    # gateway MUST match what the daemon sends.
    model_suffix = f"audit-daemon-cp-{suffix}"
    alias = f"github_copilot/{model_suffix}"
    app_id = f"audit-daemon-cp-{suffix}"

    # Step 1: create the alias with bogus metadata. The route override
    # below is the ONLY thing that makes dispatch work; if the resolver
    # ever falls back to the alias's metadata, we get a 4xx from openai.
    code, _ = gw("POST", "/admin/models", {
        "alias": alias,
        "provider_slug": "openai",
        "real_model_id": "gpt-99-fictitious",
        "cost_per_1k_input_tokens": 0,
        "cost_per_1k_output_tokens": 0,
        "max_context_tokens": 8192,
        "is_custom": False, "metadata": {},
    })
    if code != 201:
        s.fail("create alias", f"{code}")
        return s
    s.ok(f"alias {alias} created (metadata=openai/gpt-99-fictitious)")

    yaml_path = None
    try:
        # Step 2: route override to github_copilot.
        code, body = gw("POST", "/admin/routes", {
            "model_alias": alias, "credential_id": copilot_cred_id,
            "priority": 0,
            "provider_slug": "github_copilot",
            "real_model_id": "gpt-4o-mini",
            "compat": "openai_compat",
        })
        if code != 201:
            s.fail("create override route", f"{code} {body}")
            return s
        s.ok("route override -> github_copilot/gpt-4o-mini")

        # Step 3: build + deploy a minimal app whose brain points at alias.
        yaml_path = write_app_yaml(app_id, model_suffix)
        try:
            cli.deploy(str(yaml_path), force=True, wait=4.0)
        except Exception as exc:
            s.fail("deploy", f"{type(exc).__name__}: {exc}")
            return s
        # Confirm the app is REALLY listed before chatting -- the
        # /deploy endpoint sometimes reports success but the
        # compilation logs an error and the app never registers.
        deployed = [a for a in cli.list_apps() if a.get("app_id") == app_id]
        if not deployed:
            s.fail("deploy reported success but app not listed",
                   "compile probably failed silently")
            return s
        s.ok(f"app deployed and listed (status={deployed[0].get('status')})")

        # Step 4: chat through the daemon.
        gw_off = _tail_size(GATEWAY_LOG)
        try:
            sess = cli.chat(app_id, "Reply with one word: ALIVE", timeout=60.0)
        except Exception as exc:
            s.fail("chat raised", f"{type(exc).__name__}: {exc}")
            return s

        last = sess.last
        if last and (last.text or "").strip():
            s.ok(f"daemon returned a real response ({last.text!r})")
        else:
            s.fail("empty response", str(last))

        # Step 5: confirm gateway saw the call (any status, not just
        # 200; a transient 401 / 502 still proves the daemon went via
        # the gateway path which is what we're auditing here).
        new_gw = _tail_since(GATEWAY_LOG, gw_off)
        gw_chats = [
            ln for ln in new_gw.splitlines()
            if "/v1/chat/completions" in ln
        ]
        gw_2xx = [ln for ln in gw_chats if " 200 " in ln]
        if gw_chats:
            s.ok(f"gateway received {len(gw_chats)} chat call(s) "
                 f"({len(gw_2xx)} succeeded) -- daemon routed via gateway")
        else:
            s.fail("no gateway hit",
                   "the daemon did NOT route this turn through the gateway")

        # Step 6: confirm the chat content is real (Copilot reply with the
        # uppercase ALIVE token, not an error mention).
        text = (last.text or "").upper() if last else ""
        if "ALIVE" in text and "ERROR" not in text:
            s.ok(f"response carries the requested keyword (proves real LLM)")
        else:
            s.fail("response unrelated to prompt", str(last)[:120])

    finally:
        try:
            cli.undeploy(app_id)
            s.ok("cleanup: app undeployed")
        except Exception as exc:
            s.fail("undeploy", f"{type(exc).__name__}: {exc}")
        gw("DELETE", f"/admin/models/{alias}", None)
        if yaml_path and yaml_path.exists():
            yaml_path.unlink(missing_ok=True)
    return s


# ── S2. Cross-provider failover ─────────────────────────────────────


def section_s2_failover_via_daemon(cli: DevClient, copilot_cred_id: str) -> Section:
    s = Section("S2. Daemon -> gateway -> cross-provider failover")
    suffix = uuid.uuid4().hex[:6]
    model_suffix = f"audit-daemon-fo-{suffix}"
    alias = f"github_copilot/{model_suffix}"
    app_id = f"audit-daemon-fo-{suffix}"
    dummy_slug = ensure_dummy_provider()

    code, body = gw("POST", "/admin/credentials", {
        "provider_slug": dummy_slug,
        "label": f"dummy-{suffix}",
        "secret_data": {"value": "sk-dummy-fail"},
    })
    if code != 201:
        s.fail("create dummy cred", f"{code}")
        return s
    dummy_cred_id = body["id"]

    code, _ = gw("POST", "/admin/models", {
        "alias": alias, "provider_slug": "github_copilot",
        "real_model_id": "gpt-4o-mini",
        "cost_per_1k_input_tokens": 0, "cost_per_1k_output_tokens": 0,
        "max_context_tokens": 8192, "is_custom": False, "metadata": {},
    })
    if code != 201:
        s.fail("create alias", f"{code}")
        return s

    yaml_path = None
    try:
        # P0 = broken provider
        code, _ = gw("POST", "/admin/routes", {
            "model_alias": alias, "credential_id": dummy_cred_id,
            "priority": 0,
            "provider_slug": dummy_slug, "real_model_id": "gpt-4o-mini",
            "compat": "openai_compat",
        })
        if code != 201:
            s.fail("POST P0", f"{code}")
            return s
        s.ok("P0 (broken provider) created")
        # P1 = github_copilot
        code, _ = gw("POST", "/admin/routes", {
            "model_alias": alias, "credential_id": copilot_cred_id,
            "priority": 1,
            "provider_slug": "github_copilot", "real_model_id": "gpt-4o-mini",
        })
        if code != 201:
            s.fail("POST P1", f"{code}")
            return s
        s.ok("P1 (copilot) created")

        # Deploy app
        yaml_path = write_app_yaml(app_id, model_suffix)
        try:
            cli.deploy(str(yaml_path), force=True, wait=4.0)
        except Exception as exc:
            s.fail("deploy", f"{type(exc).__name__}: {exc}")
            return s
        deployed = [a for a in cli.list_apps() if a.get("app_id") == app_id]
        if not deployed:
            s.fail("deploy reported success but app not listed",
                   "compile probably failed silently")
            return s
        s.ok(f"app deployed and listed (status={deployed[0].get('status')})")

        # Send 5 chats. The first one will be slow because of the
        # connection-refused retry on P0. After 3 P0 failures, every
        # call goes straight to P1 with normal copilot latency.
        durations: list[float] = []
        successes = 0
        for i in range(5):
            t0 = time.perf_counter()
            try:
                sess = cli.chat(app_id, f"#{i+1}: reply ALIVE in one word",
                                timeout=120.0)
                last = sess.last
                if last and "ALIVE" in (last.text or "").upper():
                    successes += 1
                    durations.append((time.perf_counter() - t0) * 1000)
                    s.ok(f"chat #{i+1} -> {last.text[:40]!r} "
                         f"({durations[-1]:.0f}ms)")
                else:
                    s.fail(f"chat #{i+1}: empty/wrong",
                           str(last)[:120] if last else "no response")
            except Exception as exc:
                s.fail(f"chat #{i+1}: raised",
                       f"{type(exc).__name__}: {exc}")

        if successes == 5:
            s.ok(f"5/5 chats succeeded across the failover boundary "
                 f"(p50={statistics.median(durations):.0f}ms, "
                 f"first={durations[0]:.0f}ms)")
        else:
            s.fail(f"only {successes}/5 chats succeeded",
                   "failover did not converge fully")

        # Verify P0 got marked unhealthy
        code, lst = gw("GET", f"/admin/routes?model_alias={alias}")
        if code == 200:
            for r in lst.get("rows", []):
                if r["priority"] == 0:
                    if r["consecutive_failures"] >= 1:
                        s.ok(f"P0 saw {r['consecutive_failures']} failure(s) "
                             f"(blocked={r['is_blocked']})")
                    else:
                        s.fail("P0 never saw a failure",
                               "the resolver may have skipped P0 entirely")

    finally:
        try:
            cli.undeploy(app_id)
            s.ok("cleanup: app undeployed")
        except Exception as exc:
            s.fail("undeploy", f"{type(exc).__name__}: {exc}")
        gw("DELETE", f"/admin/models/{alias}", None)
        gw("DELETE", f"/admin/credentials/{dummy_cred_id}", None)
        if yaml_path and yaml_path.exists():
            yaml_path.unlink(missing_ok=True)
    return s


# ── Main ────────────────────────────────────────────────────────────


def main() -> int:
    print("=" * 70)
    print("  CROSS-PROVIDER E2E -- DAEMON (DevClient) -> GATEWAY -> ROUTE")
    print("=" * 70)
    print(f"  Daemon:  {DAEMON}")
    print(f"  Gateway: {GATEWAY}")
    print()

    copilot_cred_id = find_copilot_credential()
    if not copilot_cred_id:
        print("ERROR: no github_copilot credential found.")
        return 2

    cli = DevClient.with_token(
        CREDS["access_token"], daemon_url=DAEMON, timeout=120.0,
    )
    print(f"DevClient connected to daemon ({DAEMON})")
    print(f"Copilot credential: {copilot_cred_id[:8]}...")

    sections = []
    sections.append(section_s1_cross_provider_via_daemon(cli, copilot_cred_id))
    sections.append(section_s2_failover_via_daemon(cli, copilot_cred_id))

    total = sum(len(s.checks) for s in sections)
    passed = sum(1 for s in sections for c in s.checks if c[1])
    print("\n" + "=" * 70)
    for s in sections:
        print(s.render())
    print("\n" + "=" * 70)
    print(f"  TOTAL: {passed}/{total} ({sum(1 for s in sections if s.passed())}/{len(sections)} sections)")
    print("=" * 70)
    return 0 if all(s.passed() for s in sections) else 1


if __name__ == "__main__":
    sys.exit(main())

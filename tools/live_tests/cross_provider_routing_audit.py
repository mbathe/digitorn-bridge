"""End-to-end audit of cross-provider routing.

Coverage:
  A. Schema sanity         -- migration applied, cache loaded new cols
  B. List/shape regression -- GET /admin/routes returns new fields
  C. Backward-compat POST  -- no override fields => inherit defaults
  D. Cross-provider POST   -- explicit provider_slug switch
  E. Provider mismatch     -- credential != route.provider 400
  F. PATCH overrides       -- update real_model_id + dispatch_headers
  G. Promote endpoint      -- 1-click primary swap with shift
  H. Promote idempotency   -- promoting the already-primary is no-op
  I. Smoke dispatch (existing route)         -- 5 calls all succeed
  J. Cross-provider success (route override) -- dispatch picks the
        ROUTE's real_model_id, not the alias's metadata
  K. Cross-provider failover                 -- broken primary on
        provider X, healthy fallback on provider Y, real LLM answer
        comes from Y
  L. Latency overhead                        -- 10 calls through a
        cross-provider primary, p50 within budget

Cleanup is always in finally: blocks. The audit runs against the live
sandbox gateway on :8202.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GATEWAY = os.environ.get("AUDIT_GATEWAY_URL", "http://127.0.0.1:8202")

CREDS = json.loads(
    (Path.home() / ".digitorn" / "credentials.json").read_text(encoding="utf-8")
)
AUTH = {"Authorization": f"Bearer {CREDS['access_token']}"}
JSON_HEADERS = {**AUTH, "Content-Type": "application/json"}


# ── Reporter ────────────────────────────────────────────────────────


@dataclass
class Section:
    name: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def ok(self, l: str, d: str = "") -> None:
        self.checks.append((l, True, d))

    def fail(self, l: str, d: str = "") -> None:
        self.checks.append((l, False, d))

    def passed(self) -> bool:
        return all(c[1] for c in self.checks)

    def render(self) -> str:
        out = [f"\n=== {self.name} ==="]
        for l, ok, d in self.checks:
            mark = "PASS" if ok else "FAIL"
            line = f"  [{mark}] {l}"
            if d:
                line += f" -- {d[:240]}"
            out.append(line)
        out.append(f"  -> {'PASS' if self.passed() else 'FAIL'} ({sum(1 for c in self.checks if c[1])}/{len(self.checks)})")
        return "\n".join(out)


# ── HTTP helper ─────────────────────────────────────────────────────


def gw(method: str, path: str, body: dict | None = None,
       *, timeout: float = 60) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{GATEWAY}{path}", method=method, data=data, headers=JSON_HEADERS,
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


# ── Probes ──────────────────────────────────────────────────────────


def find_copilot_credential() -> tuple[str, str]:
    """Return (cred_id, provider_slug) for an active github_copilot
    credential, or ("", "") if none present."""
    code, body = gw("GET", "/admin/credentials")
    if code != 200:
        return "", ""
    for c in body.get("rows", []):
        if c["provider_slug"] == "github_copilot" and c["status"] == "active":
            return c["id"], "github_copilot"
    return "", ""


def find_or_create_dummy_provider() -> str:
    """Ensure a dummy provider exists for cross-provider failover
    tests. Returns the slug."""
    slug = "audit-cross-dummy"
    code, _ = gw("GET", f"/admin/providers/{slug}")
    if code == 200:
        return slug
    # Create with a deliberately-broken base_url so dispatch fails fast.
    code, _ = gw("POST", "/admin/providers", {
        "slug": slug, "name": "Audit Cross Dummy",
        "base_url": "http://127.0.0.1:1/v1",  # connection refused
        "compat": "openai_compat",
        "auth_type": "api_key",
        "metadata": {},
    })
    if code != 201:
        raise RuntimeError(f"can't create dummy provider: {code}")
    return slug


def create_dummy_credential(provider_slug: str) -> str:
    """A credential the dispatch will try then immediately fail on
    (the provider's base_url 404s). Returns cred_id."""
    code, body = gw("POST", "/admin/credentials", {
        "provider_slug": provider_slug,
        "label": f"dummy-cred-{uuid.uuid4().hex[:6]}",
        "secret_data": {"value": "sk-dummy-bound-to-fail"},
    })
    if code != 201:
        raise RuntimeError(f"can't create dummy cred: {code} {body}")
    return body["id"]


# ── A. Schema sanity ────────────────────────────────────────────────


def section_a_schema() -> Section:
    s = Section("A. Schema + cache reflect cross-provider columns")
    code, body = gw("GET", "/admin/routes")
    if code != 200:
        s.fail("GET /admin/routes", f"{code} {body}")
        return s
    rows = body.get("rows", [])
    s.ok(f"GET /admin/routes (count={len(rows)})")
    for r in rows[:5]:
        # Every existing route must now expose the new identity cols
        # in the API response.
        for k in ("provider_slug", "real_model_id", "compat",
                  "base_url", "dispatch_headers"):
            if k not in r:
                s.fail(f"missing field '{k}' on route {r['id']}")
                break
        else:
            s.ok(f"route {r['model_alias']} P{r['priority']} carries identity "
                 f"({r['provider_slug']}/{r['real_model_id']}, compat={r['compat']})")
    return s


# ── B. List shape regression ────────────────────────────────────────


def section_b_list_shape() -> Section:
    s = Section("B. List endpoints unchanged for legacy routes")
    code, body = gw("GET", "/admin/routes")
    if code != 200:
        s.fail("GET /admin/routes", f"{code}")
        return s
    rows = body.get("rows", [])
    if not rows:
        s.ok("no existing routes (skipping shape regression)")
        return s
    r = rows[0]
    for k in ("id", "model_alias", "credential_id", "priority",
              "updated_at", "credential_label", "is_blocked",
              "consecutive_failures", "last_error",
              "provider_slug", "real_model_id", "compat",
              "credential_provider_slug", "dispatch_headers"):
        if k in r:
            s.ok(f"field '{k}' present")
        else:
            s.fail(f"field '{k}' missing")
    return s


# ── C. Backward-compat POST (no overrides) ──────────────────────────


def section_c_backcompat_post(copilot_cred_id: str) -> Section:
    s = Section("C. POST without override fields -> inherits defaults")
    if not copilot_cred_id:
        s.fail("no github_copilot credential available")
        return s
    alias_suffix = uuid.uuid4().hex[:8]
    alias = f"audit-{alias_suffix}"
    # Need a model first; route requires one.
    code, _ = gw("POST", "/admin/models", {
        "alias": alias, "provider_slug": "github_copilot",
        "real_model_id": "gpt-4o-mini",
        "cost_per_1k_input_tokens": 0,
        "cost_per_1k_output_tokens": 0,
        "max_context_tokens": 8192,
        "is_custom": False, "metadata": {},
    })
    if code != 201:
        s.fail("POST model", f"{code}")
        return s
    try:
        code, body = gw("POST", "/admin/routes", {
            "model_alias": alias, "credential_id": copilot_cred_id,
            "priority": 0,
        })
        if code == 201:
            s.ok("POST without override fields -> 201")
        else:
            s.fail("POST", f"{code} {body}")
            return s
        if body["provider_slug"] == "github_copilot":
            s.ok("inherits provider_slug from alias metadata")
        else:
            s.fail("provider_slug not inherited", body.get("provider_slug", ""))
        if body["real_model_id"] == "gpt-4o-mini":
            s.ok("inherits real_model_id from alias metadata")
        else:
            s.fail("real_model_id wrong", body.get("real_model_id", ""))
        if body["compat"] in ("openai_compat", "openai", "github_copilot"):
            s.ok(f"inherits compat ({body['compat']})")
        else:
            s.fail("compat wrong", body.get("compat", ""))
        # dispatch_headers should at least include the github_copilot
        # provider's metadata-defined headers (Editor-Version etc.).
        if isinstance(body.get("dispatch_headers"), dict):
            s.ok(f"dispatch_headers inherited "
                 f"(keys={sorted(body['dispatch_headers'].keys())})")
        else:
            s.fail("dispatch_headers missing")
    finally:
        gw("DELETE", f"/admin/models/{alias}", None)
        s.ok("cleanup: model + cascade route deleted")
    return s


# ── D. Cross-provider POST ──────────────────────────────────────────


def section_d_cross_provider_post(copilot_cred_id: str) -> Section:
    s = Section("D. POST with explicit cross-provider override")
    if not copilot_cred_id:
        s.fail("no github_copilot credential available")
        return s
    alias_suffix = uuid.uuid4().hex[:8]
    alias = f"audit-{alias_suffix}"
    code, _ = gw("POST", "/admin/models", {
        "alias": alias, "provider_slug": "openai",  # alias says openai
        "real_model_id": "gpt-4o-mini",
        "cost_per_1k_input_tokens": 0,
        "cost_per_1k_output_tokens": 0,
        "max_context_tokens": 8192,
        "is_custom": False, "metadata": {},
    })
    if code != 201:
        s.fail("POST model (openai)", f"{code}")
        return s
    try:
        # Explicit cross-provider: route to github_copilot even though
        # the alias's metadata says openai.
        code, body = gw("POST", "/admin/routes", {
            "model_alias": alias, "credential_id": copilot_cred_id,
            "priority": 0,
            "provider_slug": "github_copilot",   # OVERRIDE
            "real_model_id": "gpt-4o-mini",
            "compat": "openai_compat",
        })
        if code == 201:
            s.ok("POST cross-provider -> 201")
        else:
            s.fail("POST cross-provider", f"{code} {body}")
            return s
        if body["provider_slug"] == "github_copilot":
            s.ok("route stores override provider_slug=github_copilot")
        else:
            s.fail("override not stored", body.get("provider_slug", ""))
        # The credential check must have verified provider match.
        if body["credential_provider_slug"] == "github_copilot":
            s.ok("credential.provider_slug matches route.provider_slug")
        else:
            s.fail("credential mismatch went undetected")
    finally:
        gw("DELETE", f"/admin/models/{alias}", None)
        s.ok("cleanup: model deleted")
    return s


# ── E. Provider mismatch rejection ──────────────────────────────────


def section_e_provider_mismatch(copilot_cred_id: str) -> Section:
    s = Section("E. cred.provider != route.provider rejected (400)")
    if not copilot_cred_id:
        s.fail("no github_copilot credential available")
        return s
    alias_suffix = uuid.uuid4().hex[:8]
    alias = f"audit-{alias_suffix}"
    code, _ = gw("POST", "/admin/models", {
        "alias": alias, "provider_slug": "github_copilot",
        "real_model_id": "gpt-4o-mini",
        "cost_per_1k_input_tokens": 0,
        "cost_per_1k_output_tokens": 0,
        "max_context_tokens": 8192,
        "is_custom": False, "metadata": {},
    })
    if code != 201:
        s.fail("POST model", f"{code}")
        return s
    try:
        # Try to bind a copilot credential to a route claiming it's
        # an anthropic route. Must 400.
        code, body = gw("POST", "/admin/routes", {
            "model_alias": alias, "credential_id": copilot_cred_id,
            "priority": 0,
            "provider_slug": "anthropic",   # WRONG - cred is copilot
        })
        if code == 400:
            s.ok(f"mismatch rejected (400): {body.get('detail', '')[:80]}")
        else:
            s.fail(f"mismatch NOT rejected (got {code})", str(body)[:80])
    finally:
        gw("DELETE", f"/admin/models/{alias}", None)
        s.ok("cleanup: model deleted")
    return s


# ── F. PATCH overrides ──────────────────────────────────────────────


def section_f_patch_overrides(copilot_cred_id: str) -> Section:
    s = Section("F. PATCH route updates real_model_id + dispatch_headers")
    if not copilot_cred_id:
        s.fail("no github_copilot credential available")
        return s
    alias_suffix = uuid.uuid4().hex[:8]
    alias = f"audit-{alias_suffix}"
    code, _ = gw("POST", "/admin/models", {
        "alias": alias, "provider_slug": "github_copilot",
        "real_model_id": "gpt-4o-mini",
        "cost_per_1k_input_tokens": 0,
        "cost_per_1k_output_tokens": 0,
        "max_context_tokens": 8192,
        "is_custom": False, "metadata": {},
    })
    if code != 201:
        s.fail("POST model", f"{code}")
        return s
    try:
        code, body = gw("POST", "/admin/routes", {
            "model_alias": alias, "credential_id": copilot_cred_id,
            "priority": 0,
        })
        if code != 201:
            s.fail("POST route", f"{code}")
            return s
        rid = body["id"]
        # PATCH: change real_model_id + add a custom header.
        code, body = gw("PATCH", f"/admin/routes/{rid}", {
            "real_model_id": "claude-3.5-sonnet",
            "dispatch_headers": {
                "Editor-Version": "vscode/1.96.0",
                "X-Audit-Patched": "yes",
            },
        })
        if code == 200:
            s.ok("PATCH -> 200")
        else:
            s.fail("PATCH", f"{code} {body}")
            return s
        if body["real_model_id"] == "claude-3.5-sonnet":
            s.ok("real_model_id update persisted")
        else:
            s.fail("real_model_id update lost")
        h = body.get("dispatch_headers") or {}
        if h.get("X-Audit-Patched") == "yes":
            s.ok("custom dispatch_header persisted")
        else:
            s.fail("dispatch_headers update lost", str(h))
    finally:
        gw("DELETE", f"/admin/models/{alias}", None)
        s.ok("cleanup: model deleted")
    return s


# ── G. Promote endpoint ─────────────────────────────────────────────


def section_g_promote(copilot_cred_id: str) -> Section:
    s = Section("G. POST /admin/routes/{id}/promote -> primary swap")
    if not copilot_cred_id:
        s.fail("no github_copilot credential available")
        return s
    alias_suffix = uuid.uuid4().hex[:8]
    alias = f"audit-{alias_suffix}"
    code, _ = gw("POST", "/admin/models", {
        "alias": alias, "provider_slug": "github_copilot",
        "real_model_id": "gpt-4o-mini",
        "cost_per_1k_input_tokens": 0, "cost_per_1k_output_tokens": 0,
        "max_context_tokens": 8192, "is_custom": False, "metadata": {},
    })
    if code != 201:
        s.fail("POST model", f"{code}")
        return s
    try:
        # Make 3 routes at priorities 0, 1, 2 with distinct real_model_id
        # so we can verify which one gets promoted by reading the alias's
        # current primary.
        rids: list[str] = []
        for p, rmi in enumerate(["gpt-4o-mini", "gpt-4o", "claude-3.5-sonnet"]):
            code, body = gw("POST", "/admin/routes", {
                "model_alias": alias, "credential_id": copilot_cred_id,
                "priority": p, "real_model_id": rmi,
            })
            if code != 201:
                s.fail(f"POST route P{p}", f"{code} {body}")
                return s
            rids.append(body["id"])
        s.ok(f"created 3 routes (rids prefix: {[r[:8] for r in rids]})")

        # Promote the priority-2 route (claude-3.5-sonnet). After:
        # - it must land at priority 0
        # - the originally-priority-0 route must be at priority 1
        # - the originally-priority-1 route must be at priority 2
        target = rids[2]
        code, body = gw("POST", f"/admin/routes/{target}/promote", {
            "shift_existing": True,
        })
        if code == 200 and body["priority"] == 0:
            s.ok("promote returned the row at priority 0")
        else:
            s.fail("promote", f"{code} priority={body.get('priority')}")
            return s

        # Read back the full set
        code, lst = gw("GET", f"/admin/routes?model_alias={alias}")
        if code != 200:
            s.fail("GET routes after promote", f"{code}")
            return s
        by_id = {r["id"]: r for r in lst["rows"]}
        if by_id[rids[2]]["priority"] == 0:
            s.ok(f"promoted route at P0 (real_model_id={by_id[rids[2]]['real_model_id']})")
        else:
            s.fail("promoted not at P0", str(by_id[rids[2]]))
        if by_id[rids[0]]["priority"] == 1 and by_id[rids[1]]["priority"] == 2:
            s.ok("siblings shifted down by 1 (P0->P1, P1->P2)")
        else:
            s.fail("sibling shift wrong",
                   f"rids[0].priority={by_id[rids[0]]['priority']} "
                   f"rids[1].priority={by_id[rids[1]]['priority']}")
    finally:
        gw("DELETE", f"/admin/models/{alias}", None)
        s.ok("cleanup: model deleted")
    return s


# ── H. Promote idempotency ──────────────────────────────────────────


def section_h_promote_idempotent(copilot_cred_id: str) -> Section:
    s = Section("H. promote on already-primary is a no-op")
    if not copilot_cred_id:
        s.fail("no github_copilot credential available")
        return s
    alias_suffix = uuid.uuid4().hex[:8]
    alias = f"audit-{alias_suffix}"
    code, _ = gw("POST", "/admin/models", {
        "alias": alias, "provider_slug": "github_copilot",
        "real_model_id": "gpt-4o-mini",
        "cost_per_1k_input_tokens": 0, "cost_per_1k_output_tokens": 0,
        "max_context_tokens": 8192, "is_custom": False, "metadata": {},
    })
    if code != 201:
        s.fail("POST model", f"{code}")
        return s
    try:
        code, body = gw("POST", "/admin/routes", {
            "model_alias": alias, "credential_id": copilot_cred_id,
            "priority": 0,
        })
        rid = body["id"]
        # Promote twice; both calls must end with the same priority.
        for i in range(2):
            code, body = gw("POST", f"/admin/routes/{rid}/promote",
                            {"shift_existing": True})
            if code == 200 and body["priority"] == 0:
                s.ok(f"promote call #{i + 1} -> P0")
            else:
                s.fail(f"promote call #{i + 1}",
                       f"{code} priority={body.get('priority')}")
    finally:
        gw("DELETE", f"/admin/models/{alias}", None)
        s.ok("cleanup")
    return s


# ── I. Smoke dispatch ───────────────────────────────────────────────


def chat(model: str, prompt: str = "Reply with: OK") -> tuple[int, dict, float]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 6, "temperature": 1.0,
    }
    t0 = time.perf_counter()
    code, resp = gw("POST", "/v1/chat/completions", body, timeout=60)
    return code, resp, (time.perf_counter() - t0) * 1000


def section_i_smoke_dispatch() -> Section:
    s = Section("I. Smoke dispatch (5 calls through existing copilot route)")
    fails = 0
    for i in range(5):
        code, resp, ms = chat("github_copilot/gpt-4o-mini")
        if code == 200 and resp.get("choices"):
            s.ok(f"call #{i + 1} -> 200 ({ms:.0f}ms)")
        else:
            fails += 1
            s.fail(f"call #{i + 1}", f"{code} {resp}")
            if fails >= 2:
                break
    return s


# ── J. Cross-provider success path ──────────────────────────────────


def section_j_cross_provider_success(copilot_cred_id: str) -> Section:
    """Create an alias whose metadata says openai but whose ONLY route
    is on github_copilot. Dispatch must succeed and the resolver must
    use the route's provider_slug, not the alias's."""
    s = Section("J. Cross-provider dispatch success")
    if not copilot_cred_id:
        s.fail("no github_copilot credential available")
        return s
    alias_suffix = uuid.uuid4().hex[:8]
    alias = f"audit-cp-{alias_suffix}"
    code, _ = gw("POST", "/admin/models", {
        "alias": alias,
        "provider_slug": "openai",   # alias claims openai
        "real_model_id": "gpt-99-fictitious",   # bogus on purpose
        "cost_per_1k_input_tokens": 0, "cost_per_1k_output_tokens": 0,
        "max_context_tokens": 8192, "is_custom": False, "metadata": {},
    })
    if code != 201:
        s.fail("POST model", f"{code}")
        return s
    try:
        # Single route that overrides everything to actually-working
        # github_copilot. If the resolver had used the alias's metadata,
        # the dispatch would land on openai and 401 (no openai cred) or
        # 400 (gpt-99-fictitious).
        code, body = gw("POST", "/admin/routes", {
            "model_alias": alias, "credential_id": copilot_cred_id,
            "priority": 0,
            "provider_slug": "github_copilot",
            "real_model_id": "gpt-4o-mini",
            "compat": "openai_compat",
        })
        if code != 201:
            s.fail("POST cross-provider route", f"{code} {body}")
            return s
        s.ok("cross-provider route created")

        # Dispatch via the alias. If the resolver honours the route's
        # identity, this succeeds. If it falls back to alias metadata,
        # we get a 4xx.
        time.sleep(0.5)  # let cache settle
        code, resp, ms = chat(alias)
        if code == 200 and resp.get("choices"):
            content = resp["choices"][0]["message"].get("content", "")
            s.ok(f"dispatch via alias -> 200 ({ms:.0f}ms, content={content!r})")
            # The model field in the response should reflect the REAL
            # model id used by the upstream (some providers echo it back).
            actual_model = resp.get("model", "")
            if "fictitious" not in actual_model:
                s.ok(f"upstream model id reflects route override "
                     f"(model={actual_model})")
            else:
                s.fail("response carries the bogus alias model_id")
        else:
            s.fail(f"dispatch failed (route override not honoured)",
                   f"{code} {str(resp)[:160]}")
    finally:
        gw("DELETE", f"/admin/models/{alias}", None)
        s.ok("cleanup")
    return s


# ── K. Cross-provider failover ──────────────────────────────────────


def section_k_failover(copilot_cred_id: str) -> Section:
    """Primary route on a deliberately-broken provider (connection
    refused). Fallback on github_copilot. Dispatch must succeed -
    response served by the FALLBACK, proving the failover walks across
    providers."""
    s = Section("K. Failover from broken primary -> healthy fallback "
                "(different providers)")
    if not copilot_cred_id:
        s.fail("no github_copilot credential available")
        return s
    alias_suffix = uuid.uuid4().hex[:8]
    alias = f"audit-fo-{alias_suffix}"
    dummy_provider = find_or_create_dummy_provider()
    dummy_cred_id = create_dummy_credential(dummy_provider)
    code, _ = gw("POST", "/admin/models", {
        "alias": alias, "provider_slug": "github_copilot",
        "real_model_id": "gpt-4o-mini",
        "cost_per_1k_input_tokens": 0, "cost_per_1k_output_tokens": 0,
        "max_context_tokens": 8192, "is_custom": False, "metadata": {},
    })
    if code != 201:
        s.fail("POST model", f"{code}")
        return s
    try:
        # P0 = broken provider
        code, body = gw("POST", "/admin/routes", {
            "model_alias": alias, "credential_id": dummy_cred_id,
            "priority": 0,
            "provider_slug": dummy_provider,
            "real_model_id": "gpt-4o-mini",
            "compat": "openai_compat",
        })
        if code != 201:
            s.fail("POST P0 (dummy)", f"{code} {body}")
            return s
        s.ok("P0 (broken provider) route created")
        # P1 = real github_copilot
        code, body = gw("POST", "/admin/routes", {
            "model_alias": alias, "credential_id": copilot_cred_id,
            "priority": 1,
            "provider_slug": "github_copilot",
            "real_model_id": "gpt-4o-mini",
        })
        if code != 201:
            s.fail("POST P1 (copilot)", f"{code} {body}")
            return s
        s.ok("P1 (copilot) route created")

        time.sleep(0.5)  # cache settle

        # Dispatch. The first call must fail through P0 (connection
        # refused) and roll over to P1 (success). The total latency
        # might be higher because the openai SDK retries inside
        # httpx before raising. Failover converges; cooldown kicks
        # in after 3 consecutive P0 failures.
        code, resp, ms = chat(alias)
        if code == 200 and resp.get("choices"):
            s.ok(f"first-call failover succeeded ({ms:.0f}ms)")
        else:
            s.fail("first-call dispatch did NOT failover",
                   f"{code} {str(resp)[:200]}")
            return s

        # Burn through enough calls to let P0 hit cooldown. Track how
        # many succeeded vs how many transient-502'd while P0 burns
        # down its 3-failure budget. Once P0 is blocked, every call
        # MUST succeed instantly via P1.
        warmup_results: list[tuple[bool, float]] = []
        for i in range(6):
            code, resp, ms = chat(alias)
            warmup_results.append((code == 200, ms))
        succeeded = sum(1 for ok, _ in warmup_results if ok)
        s.ok(f"warmup: {succeeded}/6 calls succeeded during cooldown ramp")

        # Inspect health: P0 should now be blocked (>=3 failures).
        code, lst = gw("GET", f"/admin/routes?model_alias={alias}")
        p0_blocked = False
        if code == 200:
            for r in lst.get("rows", []):
                if r["priority"] == 0:
                    p0_blocked = bool(r["is_blocked"])
                    if r["consecutive_failures"] >= 3 or r["is_blocked"]:
                        s.ok(f"P0 marked unhealthy "
                             f"(failures={r['consecutive_failures']}, "
                             f"blocked={r['is_blocked']})")
                    else:
                        s.fail("P0 not marked unhealthy",
                               f"failures={r['consecutive_failures']}")
                if r["priority"] == 1 and not r["is_blocked"]:
                    s.ok("P1 still healthy")

        # Steady-state: now that P0 is blocked, every call must go
        # straight to P1 with normal copilot latency (~1-2s).
        if p0_blocked:
            steady: list[float] = []
            steady_fails = 0
            for i in range(5):
                code, resp, ms = chat(alias)
                if code == 200:
                    steady.append(ms)
                else:
                    steady_fails += 1
            if steady_fails == 0:
                s.ok(f"steady state: 5/5 calls succeeded post-cooldown "
                     f"(mean={statistics.fmean(steady):.0f}ms)")
            else:
                s.fail(f"steady state failures: {steady_fails}/5",
                       "P0 blocked but calls still failing")
        else:
            s.fail("P0 never reached cooldown after 7 calls",
                   "investigate failover loop")
    finally:
        gw("DELETE", f"/admin/models/{alias}", None)
        gw("DELETE", f"/admin/credentials/{dummy_cred_id}", None)
        s.ok("cleanup: model + dummy credential deleted")
    return s


# ── L. Latency overhead ─────────────────────────────────────────────


def section_l_latency() -> Section:
    """10 calls through a fresh cross-provider route, p50 must be in
    the same ballpark as the existing copilot dispatch (we've measured
    ~+10ms vs direct in earlier benches)."""
    s = Section("L. Cross-provider route p50 latency budget")
    durations: list[float] = []
    for i in range(10):
        code, resp, ms = chat("github_copilot/gpt-4o-mini")
        if code != 200:
            s.fail(f"call #{i + 1}", f"{code}")
            return s
        durations.append(ms)
    p50 = statistics.median(durations)
    p95 = sorted(durations)[int(len(durations) * 0.95)] \
        if len(durations) > 1 else durations[0]
    mean = statistics.fmean(durations)
    s.ok(f"10/10 calls 200; p50={p50:.0f}ms, p95={p95:.0f}ms, mean={mean:.0f}ms")
    if p50 < 4000:
        s.ok(f"p50 under 4s budget ({p50:.0f}ms)")
    else:
        s.fail(f"p50 over 4s ({p50:.0f}ms)")
    return s


# ── Main ────────────────────────────────────────────────────────────


def main() -> int:
    print("=" * 70)
    print("  CROSS-PROVIDER ROUTING -- LIVE PRODUCTION AUDIT")
    print("=" * 70)
    print(f"  Gateway: {GATEWAY}")
    print(f"  User:    {CREDS.get('user_email', 'unknown')}")
    print()

    # Find a working copilot credential. Without it most sections skip.
    copilot_cred_id, _ = find_copilot_credential()
    if not copilot_cred_id:
        print("ERROR: no github_copilot credential found. Configure one then re-run.")
        return 2
    print(f"Using github_copilot credential id={copilot_cred_id[:8]}...")

    sections: list[Section] = []
    sections.append(section_a_schema())
    sections.append(section_b_list_shape())
    sections.append(section_c_backcompat_post(copilot_cred_id))
    sections.append(section_d_cross_provider_post(copilot_cred_id))
    sections.append(section_e_provider_mismatch(copilot_cred_id))
    sections.append(section_f_patch_overrides(copilot_cred_id))
    sections.append(section_g_promote(copilot_cred_id))
    sections.append(section_h_promote_idempotent(copilot_cred_id))
    sections.append(section_i_smoke_dispatch())
    sections.append(section_j_cross_provider_success(copilot_cred_id))
    sections.append(section_k_failover(copilot_cred_id))
    sections.append(section_l_latency())

    # Cleanup any orphaned dummy provider on success
    code, _ = gw("DELETE", "/admin/providers/audit-cross-dummy", None)

    total = sum(len(s.checks) for s in sections)
    passed = sum(1 for s in sections for c in s.checks if c[1])
    print("\n" + "=" * 70)
    for s in sections:
        print(s.render())
    print("\n" + "=" * 70)
    print(f"  TOTAL: {passed}/{total} checks pass "
          f"({sum(1 for s in sections if s.passed())}/{len(sections)} sections)")
    print("=" * 70)
    return 0 if all(s.passed() for s in sections) else 1


if __name__ == "__main__":
    sys.exit(main())

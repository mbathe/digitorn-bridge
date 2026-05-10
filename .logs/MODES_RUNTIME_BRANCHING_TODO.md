# Modes - Runtime Branching TODO

> **Goal.** Wire the new `runtime.modes: dict[str, ModeDef]` schema feature
> end-to-end so that picking a mode in the chat composer actually changes
> the agent's behavior at dispatch time (system prompt, tool grants,
> max_turns, timeout, behavior profile, workspace mode).
>
> **Why this file exists.** The schema, the 7 builtin YAMLs and the
> docs are done. The runtime side (the part that makes the mode *do*
> something) is **not** wired yet. Splitting it into a fresh session
> to keep context clean.
>
> **Created:** 2026-05-08

---

## TL;DR for the next session

You can deploy an app with `runtime.modes`, the chat picker shows up,
the user can click "Plan" / "Auto" / etc., **but the daemon ignores
the choice**. Every turn currently runs with the app's default config
regardless of selected mode.

This file lists exactly what to add, where, in what order, with the
verification step at each milestone.

---

## State on 2026-05-08 - what is done

| Layer | File | Status |
|---|---|---|
| Pydantic schema (`ModeDef`, `RuntimeBlock.modes`) | `packages/digitorn/core/app/schema.py` (lines ~2345-2502) | ✅ done, `extra: forbid` |
| YAML compilation (modes survive into `compiled.runtime.modes`) | via standard compiler | ✅ verified by validation script |
| Picker IDs exposed to client | `packages/digitorn/core/app/manager_v2/_models.py::_extract_mode_ids` + `summary()` returns `modes: list[str]` | ✅ done |
| `default_mode` resolver | `_resolve_default_mode` (auto > first > none) | ✅ done |
| `summary()` emits `default_mode` | added 2026-05-08 second pass | ✅ done |
| `AppSummary` Pydantic carries `modes` + `default_mode` | `packages/digitorn/core/api/apps_v2/_shared.py` | ✅ done |
| `SessionMessageRequest.mode` (POST /messages body) | `_shared.py` | ✅ accepted, logged at handler entry, NOT yet consumed by runtime |
| `CreateSessionRequest.mode` (POST /sessions body) | `_shared.py` + forwarded in `sessions.py` to `SessionMessageRequest` | ✅ done |
| Web client picker | `digitorn_web/src/components/chat/mode-picker.tsx` reads `activeApp.modes`, snaps to `defaultMode` on app switch | ✅ done |
| Web `AppSummary.defaultMode` | `digitorn_web/src/models/app-summary.ts` | ✅ done |
| Web chat-store sends `mode` on all 4 POST sites (create, send, edit-resend, retry) | `digitorn_web/src/stores/chat.ts` | ✅ done |
| Web `selectedMode` widened to `string` | `chat.ts` | ✅ done |
| Flutter client picker | `digitorn_client/lib/ui/chat/mode_picker.dart` reads `activeApp.modes`, snaps to `defaultMode` on app switch, clears `selectedMode` to `''` for chat-only apps | ✅ done |
| Flutter `AppSummary.defaultMode` | `digitorn_client/lib/models/app_summary.dart` | ✅ done |
| Flutter `AppState._selectedMode` defaults to `''` | `lib/main.dart` | ✅ done |
| Flutter `SessionService.enqueueMessage` / `createAndSetSession` / `sendMessage` accept optional `mode` param | `lib/services/session_service.dart` | ✅ done |
| Flutter `ChatPanel._send` reads `appState.selectedMode` and forwards to all 3 send sites | `lib/ui/chat/chat_panel.dart` | ✅ done |
| Builtin YAMLs converted | 7 files under `packages/digitorn/builtins/*/app.yaml` | ✅ done, all 7 validate |
| Docs | `docs-site/docs/language/02-app-config.md` (AppMeta row removed, new `### runtime.modes` section ~line 200) | ✅ done |

## State on 2026-05-08 - what is NOT done (the actual work)

The grep that proves it:

```bash
$ grep -r "ModeDef\|runtime\.modes\|selectedMode\|active_mode\|mode_id" packages/digitorn/core/
packages/digitorn/core/app/schema.py        # definition only
packages/digitorn/core/app/manager_v2/_models.py  # _extract_mode_ids only
```

**Nothing else reads `runtime.modes`.** Specifically:

- `POST /messages` and `POST /sessions/{sid}/messages` accept no `mode` field.
- `agent_loop.py` does not branch on a mode.
- `bootstrap.py` builds the tool index from `tools.grant` without considering modes.
- The behavior engine profile is fixed at `security.behavior.profile`, never overridden by mode.
- There is no concept of a "default mode" anywhere on the server.

---

## Architecture target - what "wired correctly" looks like

At turn-dispatch time, given:

- `compiled` (the deployed `AppDefinition`)
- the `mode_id` the client sent (or fallback to default)

The daemon must produce an `EffectiveTurnConfig` by sparse-overriding
the app's normal config with the active mode's overrides:

```python
def merge_mode(compiled, mode_id) -> EffectiveTurnConfig:
    runtime = compiled.runtime
    mode = runtime.modes.get(mode_id) if mode_id else None
    if mode is None:  # no modes declared, or unknown id
        return EffectiveTurnConfig.from_compiled(compiled)
    return EffectiveTurnConfig(
        max_turns       = mode.max_turns       or runtime.max_turns,
        timeout         = mode.timeout         or runtime.timeout,
        workspace_mode  = mode.workspace_mode  or compiled.ui.workspace.mode,
        system_prompt_suffix = mode.system_prompt,           # appended, not replacing
        tool_grants     = mode.tool_grants     or compiled.tools.grant,
        behavior_profile= mode.behavior_profile or compiled.security.behavior.profile,
    )
```

Default mode policy (decision needed in step 4):

- Option A: first key of `runtime.modes` (insertion order).
- Option B: `auto` if present, else first key.
- Option C: explicit `runtime.default_mode: str` field (cleanest, but new schema field).
- **Recommended: Option B for now**, no new schema field, predictable, matches user mental model ("auto is the default agent loop").

---

## The 4-step plan

Do these IN ORDER. Each step ends with a live-test command. Do not move
on until the live test passes. (Reminder: live-test-or-it-doesn't-exist
is in the user's memory.)

---

### STEP 1 - Plumb `mode` through the API

**Files to touch:**

1. `packages/digitorn/core/api/apps_v2/messages.py` (or wherever `POST /sessions/{sid}/messages` is defined - grep for the route).
2. The Pydantic body model for the messages endpoint.

**Add:**

```python
class SendMessageBody(BaseModel):
    text: str
    mode: str | None = None      # NEW
    images: list[ImageRef] = []
    # ... whatever else exists
```

Then in the handler, forward `body.mode` to the session/turn dispatcher.
Find the dispatcher with: `grep -rn "def send_message\|enqueue_turn\|run_turn" packages/digitorn/core`.

**Done when:**

```bash
curl -X POST .../api/apps/digitorn-code/sessions/$SID/messages \
  -H "Authorization: Bearer $JWT" \
  -d '{"text":"hi","mode":"plan"}'
# returns 200, daemon log shows "received turn with mode=plan"
```

(You may need to add a one-line `_log.info("turn mode=%s", body.mode)` in
the handler to verify the value travels.)

---

### STEP 2 - Build the merge layer

**Files to touch:**

1. `packages/digitorn/core/runtime/agent_loop.py` (probably `run_turn` or
   `_prepare_turn_context`). Grep: `grep -n "max_turns\|system_prompt\|tool_grants" packages/digitorn/core/runtime/agent_loop.py`.
2. `packages/digitorn/core/runtime/bootstrap.py` if the tool-index build
   needs a re-pass when `mode.tool_grants` is non-empty.

**Add a helper (new file or in agent_loop.py):**

```python
# packages/digitorn/core/runtime/mode_merge.py
from dataclasses import dataclass
from digitorn.core.app.schema import AppDefinition, ModeDef, CapabilityGrant

@dataclass
class EffectiveTurn:
    max_turns: int
    timeout: float
    workspace_mode: str | None
    system_prompt_suffix: str
    tool_grants: list[CapabilityGrant]
    behavior_profile: str
    active_mode_id: str | None

def resolve_mode(compiled: AppDefinition, mode_id: str | None) -> EffectiveTurn:
    runtime = compiled.runtime
    modes = runtime.modes or {}

    # Default policy: option B (auto > first > none)
    if not mode_id:
        if "auto" in modes:
            mode_id = "auto"
        elif modes:
            mode_id = next(iter(modes))

    mode: ModeDef | None = modes.get(mode_id) if mode_id else None
    if mode is None:
        return EffectiveTurn(
            max_turns=runtime.max_turns,
            timeout=runtime.timeout,
            workspace_mode=None,
            system_prompt_suffix="",
            tool_grants=compiled.tools.capabilities.grant,
            behavior_profile=getattr(compiled.security.behavior, "profile", ""),
            active_mode_id=None,
        )
    return EffectiveTurn(
        max_turns=mode.max_turns or runtime.max_turns,
        timeout=mode.timeout or runtime.timeout,
        workspace_mode=mode.workspace_mode,
        system_prompt_suffix=mode.system_prompt,
        tool_grants=mode.tool_grants or compiled.tools.capabilities.grant,
        behavior_profile=mode.behavior_profile or getattr(compiled.security.behavior, "profile", ""),
        active_mode_id=mode_id,
    )
```

**Done when:** unit test in `tests/test_mode_merge.py` covers:

- empty `runtime.modes` → returns app defaults, `active_mode_id is None`
- mode with empty fields → returns app defaults except `active_mode_id`
- mode with `max_turns=8` → effective is 8
- mode with `tool_grants=[fs:read]` → effective is just that grant
- unknown mode_id with non-empty modes → falls back to default policy

---

### STEP 3 - Apply the EffectiveTurn

This is the meat. Each field needs a different injection point.

**3.a - `system_prompt_suffix`**
Find the system prompt assembly: `grep -n "system_prompt\|build_system_prompt" packages/digitorn/core/runtime/`. Most likely in `context_builder/prompt.py` or `agent_loop.py`. Append the suffix AFTER the agent's normal prompt and AFTER the `# Tool Usage Instructions` block, with a clear separator:

```python
if effective.system_prompt_suffix:
    prompt += f"\n\n# Active mode: {effective.active_mode_id}\n{effective.system_prompt_suffix}"
```

**3.b - `max_turns` and `timeout`**
Find the loop bound: `grep -n "max_turns\|timeout" packages/digitorn/core/runtime/agent_loop.py`. Replace `runtime.max_turns` / `runtime.timeout` with `effective.max_turns` / `effective.timeout` in the per-turn path only (NOT in app-level deploy validation).

**3.c - `tool_grants`**
This is the trickiest one. The tool index is normally built once in `bootstrap.py`. Two options:

- **Option A (cheap):** rebuild the tool index per turn when `mode.tool_grants` is non-empty. Slow but isolated.
- **Option B (correct):** pre-build one index per declared mode at deploy time, cache them in `compiled._mode_tool_indexes: dict[str, ToolIndex]`, swap at dispatch.

Recommended **Option B**. Add to `bootstrap.py` after the main tool index is built:

```python
compiled._mode_tool_indexes = {}
for mode_id, mode_def in (compiled.runtime.modes or {}).items():
    if mode_def.tool_grants:
        compiled._mode_tool_indexes[mode_id] = build_index_for_grants(
            modules=compiled.tools.modules,
            grants=mode_def.tool_grants,
            max_risk=compiled.tools.capabilities.max_risk_level,
        )
```

Then in agent_loop, before the LLM call:

```python
tool_index = compiled._mode_tool_indexes.get(effective.active_mode_id) or compiled._default_tool_index
```

**3.d - `behavior_profile`**
`grep -n "behavior_module\|profile" packages/digitorn/core/runtime/agent_loop.py`. Where the per-turn behavior state is set up, push the override:

```python
if effective.behavior_profile and ctx.behavior_module:
    ctx.behavior_module.set_profile_for_turn(effective.behavior_profile)
```

May need to add `set_profile_for_turn` to the behavior module if it does not exist.

**3.e - `workspace_mode`**
This affects the client UI, not the agent loop. Echo it back in the SSE stream `turn_start` event so the client can hide/show the workspace pane. Find the event emit: `grep -n "turn_start\|emit.*turn" packages/digitorn/core/runtime/`.

```python
yield SSE("turn_start", {
    "mode": effective.active_mode_id,
    "workspace_mode": effective.workspace_mode,
    # ...
})
```

**Done when:** end-to-end test passes:

```bash
# Deploy digitorn-code (which has ask/plan/auto)
digitorn dev deploy packages/digitorn/builtins/digitorn-code/app.yaml

# Send a message in "ask" mode - check log shows ask system prompt + 8 max_turns + 4-tool grant
digitorn dev chat digitorn-code -m "what does this repo do?" --mode ask

# Send same in "auto" - check log shows full grant + 200 max_turns
digitorn dev chat digitorn-code -m "fix bug X" --mode auto
```

(Note: `digitorn dev chat` may need a `--mode` flag added. See `packages/digitorn/core/cli/dev.py`.)

---

### STEP 4 - Default mode policy + client wiring

**4.a - Server tells client which mode is the default.**
Extend `_extract_mode_ids` in `manager_v2/_models.py` to also return the
default mode id, and add it to the summary:

```python
def _resolve_default_mode(modes: dict) -> str | None:
    if not modes:
        return None
    if "auto" in modes:
        return "auto"
    return next(iter(modes))

# in summary():
"modes": _extract_mode_ids(self.compiled),
"default_mode": _resolve_default_mode(getattr(runtime, "modes", {}) or {}),
```

Add `default_mode: str | None = None` to `AppSummary` in `_shared.py`.

**4.b - Client uses `default_mode` instead of hardcoded `"auto"`.**

- Web: `digitorn_web/src/stores/chat-store.ts` (or wherever `selectedMode` is initialized) should fall back to `activeApp.defaultMode || activeApp.modes[0] || "auto"`.
- Flutter: same in `digitorn_client/lib/ui/chat/composer_state.dart` (or equivalent provider).

**4.c - Client always sends `mode` on send.** Verify both clients
include `mode: selectedMode` in the body of POST /messages.

**Done when:**

- A user opening `digitorn-code` for the first time sees "Auto" pre-selected (because `auto` is in modes).
- A user opening a hypothetical `runtime.modes: {ask: {...}}` (single mode) app sees no picker AND the message body still carries `mode: "ask"`.
- An app with `runtime.modes: {}` sees no picker AND the body has `mode: null`, daemon falls back to app defaults.

---

## Pitfalls to remember

1. **Mode merging is sparse, not replacing.** `mode.system_prompt` is appended, not substituted. `mode.tool_grants` empty means inherit, not "no tools".
2. **Do not validate modes at deploy time using current capabilities.** A mode can grant a subset of `tools.grant`, never more. Add a deploy-time check that `mode.tool_grants ⊆ tools.grant` (warn, do not block, in case an app wants to grant a tool only inside a specific mode).
3. **`extra: forbid` everywhere.** The schema rejects unknown fields. Don't try to add extra runtime metadata to `ModeDef` without updating the model.
4. **The client may send `mode` for an app that has no modes.** Guard: if `mode_id not in runtime.modes`, ignore (do not 400).
5. **OAuth / credentials per-mode is NOT in scope.** All modes share the app's credentials. If you need per-mode credentials later, that is a separate epic.
6. **Sub-agents inherit the active mode? NO.** `runtime.modes` overrides apply to the coordinator turn only. Sub-agents (`agent_spawn`) get the app's default config. If users later ask for "the explore sub-agent runs in plan mode", that is also a separate feature.

---

## Pointers - schema reference

```text
packages/digitorn/core/app/schema.py
  line 35-110     class AppMeta             (no `modes` field anymore)
  line 130-141    class CapabilityGrant     (used by ModeDef.tool_grants)
  line 2345-2449  class ModeDef             (the new model)
  line 2452+      class RuntimeBlock        (.modes field at ~line 2489)
```

## Pointers - YAMLs that exercise modes

```text
packages/digitorn/builtins/digitorn-code/app.yaml          # ask, plan, auto
packages/digitorn/builtins/digitorn-builder/app.yaml       # ask, plan, auto
packages/digitorn/builtins/digitorn-clone/app.yaml         # ask, plan, auto
packages/digitorn/builtins/digitorn-react-sandbox/app.yaml # plan, auto
packages/digitorn/builtins/digitorn-chat/app.yaml          # NO modes (chat only)
packages/digitorn/builtins/copilot-smoke/app.yaml          # NO modes
packages/digitorn/builtins/digitorn-deepresearch/app.yaml  # NO modes
```

## Quick re-validation

After any schema or YAML edit:

```powershell
py -3.12 -c "
import yaml; from digitorn.core.app.schema import AppDefinition; from pathlib import Path
root = Path(r'packages/digitorn/builtins')
for y in sorted(root.glob('*/app.yaml')):
    raw = yaml.safe_load(y.read_text(encoding='utf-8'))
    AppDefinition.model_validate(raw); print('OK', y.parent.name)
"
```

All 7 must print OK. Already passing on 2026-05-08.

---

## Out-of-scope (do not touch in this branch)

- Adding `runtime.default_mode` as a first-class schema field. We use the
  Option-B implicit policy (`auto` > first > none) for now.
- Per-mode model swap (e.g. `auto` uses Sonnet, `plan` uses Haiku).
  Discussed but deferred - users can still override `brain` per agent.
- Mode-aware quick prompts (different prompts shown depending on selected mode).
- Mode change mid-turn. Once a turn is dispatched, the effective config is
  frozen for that turn. The user must wait for the turn to finish before
  switching modes takes effect.

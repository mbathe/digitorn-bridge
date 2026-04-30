# Queue / dispatch redesign — design doc

**Status**: draft, awaiting user validation. No code touched yet.
**Date**: 2026-04-29.
**Constraint**: HTTP contract (`POST /messages`, `POST /sessions`, `POST /abort`, response shapes) is **frozen**. Internal logic only.

---

## 1. Today's problem in one paragraph

Three independent code paths all do the same thing — "dispatch a queued or fast-path message via `manager.chat()`, classify errors, emit events, write queue terminal state". They've drifted. Each one has its own logging level, its own event-type promotion logic (or lack of), its own heartbeat (or none), its own queue lifecycle. Bug fixes land in one path; the other two stay broken. Net effect: same user action gives different UX depending on which path runs, missing credentials cascade through the queue logging stack-traces for an expected scenario, and there's no way to "pause" the queue while a user fixes their key.

The redesign collapses the 3 paths to **one** dispatch helper, with explicit pause/resume around credential gates.

---

## 2. The single source of truth — `_dispatch_turn`

### 2.1. Where it lives

New helper in `packages/digitorn/core/api/apps_v2/_dispatch.py` (new file, kept close to `_shared.py` since the existing dispatch logic lives there). Pure async function — no class, no global state beyond the existing `manager` / `event_bus`.

### 2.2. Signature

```python
async def dispatch_turn(
    request: Request,
    app_id: str,
    session_id: str,
    *,
    entry: TurnEntry,             # immutable input describing what to run
    user_id: str = "local",
    source: TurnSource,           # FAST | DRAIN | RESUME
) -> TurnOutcome:
    ...
```

`TurnEntry`:
```python
@dataclass(frozen=True)
class TurnEntry:
    correlation_id: str
    message: str
    workspace: str | None = None
    image_refs: list[dict] | None = None
    client_message_id: str = ""
    queue_row_id: str = ""        # empty for fast-path with no DB row
    position: int = 0             # for message_started.payload.position
```

`TurnSource`:
```python
class TurnSource(str, Enum):
    FAST = "fast"      # POST /messages, session was idle
    DRAIN = "drain"    # _drain_queue_next chain after a previous turn
    RESUME = "resume"  # Socket.IO join_session reconnect
```

`TurnOutcome`:
```python
@dataclass(frozen=True)
class TurnOutcome:
    status: TurnStatus            # COMPLETED | FAILED | CANCELLED | PAUSED
    error_code: str = ""          # "" when COMPLETED; e.g. "credential_required"
    error_data: dict | None = None  # the _classify_error payload, for caller diag
    paused_reason: str = ""       # set when status == PAUSED (credential_missing, ...)
```

`TurnStatus.PAUSED` is **new**. Today there's only completed / failed / cancelled. Paused means "the queue row stays alive, do NOT chain-drain, daemon waits for an external signal (credential grant, abort) before resuming."

### 2.3. What it does internally

```
async def dispatch_turn(...):
    # 1. Pre-dispatch: emit message_started
    emit("message_started", correlation_id=entry.correlation_id, ...)

    # 2. Heartbeat — same _hb_loop as today's fast-path, now shared
    heartbeat_task = start_heartbeat(...)

    try:
        # 3. Credential check — promote to credential_required event
        try:
            await ensure_user_credentials_for_app(...)
        except CredentialMissing as exc:
            emit_credential_required(exc, correlation_id=entry.correlation_id)
            return TurnOutcome(
                status=PAUSED,
                error_code="credential_required",
                error_data=_classify_error(exc),
                paused_reason="credential_missing",
            )
        except CredentialAuthRequired as exc:
            emit_credential_auth_required(exc, ...)
            return TurnOutcome(status=PAUSED, error_code="credential_auth_required", ...)

        # 4. Run the turn
        await manager.chat(app_id, session_id, entry.message, ...)

        # 5. Detect mid-turn cancellation
        sess = await manager.get_session(...)
        if getattr(sess, "interrupted", False):
            emit("message_cancelled", correlation_id=entry.correlation_id, ...)
            return TurnOutcome(status=CANCELLED)

        # 6. Success
        emit("message_done", correlation_id=entry.correlation_id, ...)
        return TurnOutcome(status=COMPLETED)

    except asyncio.CancelledError:
        # Abort propagated — let caller handle queue cleanup
        emit("message_cancelled", ...)
        raise

    except Exception as exc:
        # 7. Real crash — classify, emit, route by code
        error_data = _classify_error(exc)
        code = error_data.get("code", "")
        log_level = _log_level_for(code)   # info for credential_*, error otherwise
        log_level("turn_failed app=%s session=%s code=%s", app_id, session_id, code)
        evt_type = "credential_required" if code in CREDENTIAL_CODES else "error"
        emit(evt_type, payload={**error_data, "correlation_id": entry.correlation_id})
        return TurnOutcome(status=FAILED, error_code=code, error_data=error_data)

    finally:
        heartbeat_task.cancel()
        await _inc_agent_turns(request, -1)
```

### 2.4. What changes vs today

- **Single error classification + promotion path.** Both `CredentialMissing` (raised pre-`chat()`) and credential errors raised from inside `chat()` (e.g. provider rejected the key mid-turn) flow through the same `_classify_error` + promotion. Today queue drain misses this.
- **Single heartbeat path.** Drain + resume paths get heartbeat for free. Today only fast-path has it.
- **Single cancellation detection.** All paths read `session.interrupted` and emit `message_cancelled` consistently.
- **`PAUSED` is a first-class outcome.** Callers know "don't chain to next queue entry" without needing to inspect error codes.
- **Log levels are derived from the error code**, not duplicated. `CredentialMissing` → `info` (expected user flow), provider crashes / unknown exceptions → `error+stack`.

---

## 3. The 3 callers, after refactor

### 3.1. Fast-path (`messages.py::_run_turn`)

Becomes:
```python
async def _run_turn():
    entry = TurnEntry(
        correlation_id=_active_correlation_id,
        message=body.message,
        workspace=_workspace,
        image_refs=_image_refs,
        client_message_id=body.client_message_id or "",
        queue_row_id=_active_queue_row_id,    # may be ""
        position=0,
    )
    outcome = await dispatch_turn(
        request, app_id, session_id,
        entry=entry, user_id=_user_id, source=TurnSource.FAST,
    )
    # Queue row terminal flip + chain drain (only if there was a row)
    if _active_queue_row_id:
        await _finalize_queue_row(outcome, ...)
```

The old 130-line function shrinks to ~30 lines. All branching on error codes is gone.

### 3.2. Drain chain (`_shared.py::_drain_queue_next::_run_next`)

```python
async def _run_next():
    outcome = await dispatch_turn(
        request, app_id, session_id,
        entry=TurnEntry.from_queue_row(entry),
        user_id=user_id, source=TurnSource.DRAIN,
    )
    if outcome.status == TurnStatus.PAUSED:
        # Critical: do NOT chain — queue stays where it is until
        # external signal (credential grant) resumes it.
        await _mark_queue_row_paused(entry.id, outcome.paused_reason)
        return  # no _drain_queue_next call
    # Normal path: terminal flip + chain
    await _finalize_queue_row(outcome, entry, ...)
    await _drain_queue_next(...)   # only when not paused
```

### 3.3. Resume drain (`manager_v2/_queue.py::drain_session_queue`)

```python
async def drain_session_queue(...):
    while True:
        entry = await _mq.next_queued(session_id)
        if entry is None: break
        outcome = await dispatch_turn(
            request, app_id, session_id,
            entry=TurnEntry.from_queue_row(entry),
            user_id=user_id, source=TurnSource.RESUME,
        )
        if outcome.status == TurnStatus.PAUSED:
            await _mark_queue_row_paused(entry.id, outcome.paused_reason)
            break  # stop loop, wait for user input
        await _finalize_queue_row(outcome, entry, ...)
```

`drain_session_queue` is also the function that **resumes** after a credential grant (see §4).

---

## 4. Pause / resume mechanics

### 4.1. Queue row state

Today the queue has these terminal states (in DB column `status`): `queued`, `running`, `completed`, `failed`, `cancelled`. We add **one new state**:

- `paused` — row was being dispatched, hit a credential gate, dispatch returned PAUSED. Daemon is awaiting external resume signal.

`paused` is **non-terminal** — the next `_drain_queue_next` / `drain_session_queue` will pick it up again. From the queue's POV it's like `queued` except it's at the head and won't be re-popped automatically (someone has to explicitly resume).

**Schema impact**: new enum value in `MessageQueueRow.status`. SQL migration: just allow the new string in the CHECK constraint (Alembic-friendly, no rename, no data move).

### 4.2. Resume signal

When the credential gate clears, the daemon needs to "kick" the queue. Trigger:

- **Existing** `credential_added` / `credential_changed` events already fire when the user grants a credential via `POST /credentials`.
- **New behaviour**: in the credential store's emit path, after firing the existing event, also kick a per-session resume:
  ```python
  for sid in sessions_with_paused_rows(provider, user_id):
      asyncio.create_task(manager.drain_session_queue(app_id, sid, user_id))
  ```

`drain_session_queue` already exists and now uses `dispatch_turn`, so resuming is "just call drain again." The paused row is re-popped, the cred check passes this time, the turn runs.

### 4.3. Cancel paused row

If the user cancels the credential modal (we already call `POST /abort?purge_queue=true`), the abort handler:
- Cancels the active asyncio task (none, since we PAUSED — no-op).
- `purge_queue=true` → calls `_mq.clear(session_id)` which transitions all `queued` + `paused` rows to `cancelled`. **Today `clear` only handles `queued`** — small change to also touch `paused`.
- Emits `message_cancelled` for the paused correlation_id so the client closes the bubble cleanly.

### 4.4. Daemon restart while a row is paused

`paused` rows survive a daemon restart (they're in the DB). On boot, `rehydrate_on_boot` is called. Two options:

- **Conservative**: leave paused rows alone. The user's grant will trigger a drain.
- **Cleanup**: convert `paused` rows older than X hours to `cancelled` so they don't hang forever.

Recommendation: conservative for now (paused = user has work to do, daemon shouldn't decide for them). Add a `purge_paused_older_than_24h` chore later if it becomes a problem.

---

## 5. Event contract (what the client sees)

### 5.1. No new event types. No payload shape changes.

All existing event types are preserved. The only behavioural change is **which type fires when** for credential issues:

| Scenario | Today fast-path emits | Today queue drain emits | After redesign (all 3 paths) |
|---|---|---|---|
| `CredentialMissing` raised pre-chat | `credential_required` | `error` | `credential_required` |
| `CredentialAuthRequired` raised pre-chat | `credential_auth_required` | `error` | `credential_auth_required` |
| Provider auth fail mid-turn (401 from OpenAI etc.) | `credential_required` (via `_classify_error`) | `error` | `credential_required` |
| Real crash (KeyError, network, etc.) | `error` | `error` | `error` |
| Insufficient billing | `error` (code: insufficient_balance) | `error` (code: insufficient_balance) | `error` (code: insufficient_balance) |
| User abort | `message_cancelled` | `message_cancelled` | `message_cancelled` |
| Normal completion | `message_done` | `message_done` | `message_done` |

The web client already handles `credential_required` everywhere it's emitted (case "credential_required" reducer at `chat.ts:1771`). It also handles `error` and routes failures to the user bubble. **No client changes required for §5**.

### 5.2. Why no new "paused" event

The PAUSED outcome is a daemon-internal state. From the client's POV:
- `credential_required` fires → modal opens → user grants OR cancels
- That's the existing UX

The client doesn't need to know "the queue row is paused on the daemon side." All it sees is: cred event → user action → either `message_started` (resumed naturally) or `message_cancelled` (after abort).

---

## 6. Edge cases checked

| Case | Handling |
|---|---|
| User aborts during a paused row | `POST /abort?purge_queue=true` → row flipped to `cancelled` → `message_cancelled` event |
| Multiple queued msgs, all need same missing cred | First one PAUSES, drain stops. Grant triggers drain → first runs, succeeds, chains naturally to next. **No more cascade**. |
| Cred granted but provider rejects (loop) | `dispatch_turn` re-raises, `_classify_error` returns `code: "credential_required"`, evt promoted, row goes back to PAUSED. Client's loop guard catches at N tries. |
| Daemon restart while paused row exists | Row survives in DB. On user grant, `drain_session_queue` re-pops it. |
| Race: user clicks RETRY before paused row processed | RETRY hits `POST /messages`. Daemon's `is_turn_running` returns False (no asyncio task running for paused row). Fast-path. **But** the paused row is still in queue, which means the new fast-path POST detects queue depth > 0 → goes into queue itself. Outcome: new msg ends up behind the paused one. **Acceptable**, matches user mental model "your previous message is paused, this one waits." Alternatively: `RETRY` from a `sendFailed=true` bubble could also send `?cancel_paused=true` to clear the paused row first. Decided in §7. |
| Resume drain while another fast-path is running | `is_turn_running` check protects. Resume kick uses the same `_dispatch_turn` which respects `_turn_semaphore`. Serialisation preserved. |

---

## 7. Open question for you

In **§6 case "Race: user clicks RETRY before paused row processed"**, two choices:

- **A — accept the queue ordering**: RETRY POST goes behind the paused row. After grant, both run in order. Works without code change beyond §3.
- **B — RETRY clears the paused row first**: client adds `?clear_paused=true` to RETRY POST. Daemon transitions the paused row to `cancelled` before accepting the new message. RETRY then takes the fast-path. Cleaner UX but adds an HTTP query param (small contract delta).

Recommendation: **A**, because:
- It doesn't touch the contract.
- It matches the user's mental model: "I sent A, it paused; I'm sending B; A will run first when I fix things."
- B adds complexity for limited gain. If we ever need it, adding a query param doesn't break old clients.

Tell me which you prefer.

---

## 8. Migration plan (how we land this safely)

1. **Step 0 — review this doc with you.** No code yet.
2. **Step 1 — add `dispatch_turn` + types.** New file `_dispatch.py`. No call sites changed yet. Verify imports compile.
3. **Step 2 — migrate fast-path** (`messages.py::_run_turn`) to call `dispatch_turn`. Smoke test: billing error, generic crash, normal completion, cred-required all behave identically to today.
4. **Step 3 — migrate drain chain** (`_shared.py::_run_next`). Smoke test + multi-message queue.
5. **Step 4 — migrate resume drain** (`drain_session_queue`). Test: kill daemon mid-turn, restart, verify the queued message resumes.
6. **Step 5 — add `paused` queue state + resume kick.** Only structural change. Tests: missing cred → row paused → grant → row runs.
7. **Step 6 — fix the "queued right after turn ends" race** (see §11). Re-order the `finally` block so the queue terminal flip + session release happen BEFORE `message_done` emit.
8. **Step 7 — physical cleanup, hard delete.** Once all 3 callers are converted and green:
   - Delete the inlined error-classification, event-emit, and heartbeat blocks from `_run_turn`, `_run_next`, `drain_session_queue`. Not commented out, not flagged with `# unused` — *deleted*.
   - Delete `_classify_error` call sites in those callers (it lives behind `dispatch_turn` now).
   - Delete `agent_turn_crashed` / `queue_drain_crashed` log lines (replaced by the unified `dispatch_turn` log). The strings disappear from grep entirely.
   - Drop the duplicated `from digitorn.core.events.envelope import OpState as _OS` imports inside the migrated branches.
   - Run a global grep to confirm no orphan references to the removed branches: `grep -rn "queue_drain_crashed\|agent_turn_crashed" packages/` should return zero hits.
   - Run a search for the helper signatures we removed (`_classify_error` should still appear ONLY in `_dispatch.py` and `_shared.py:_classify_error` itself, nowhere else).

Each step is a small, reversible diff. We commit (when you say) after each green step.

---

## 9. Storage backend — Redis only, SQL deleted

**Decisions locked 2026-04-29:**

1. **Default backend = `"redis"`.** Change the literal in `config.py:411` from `default="sql"` to `default="redis"`.
2. **`SqlQueueBackend` is deleted physically.** Not flagged as legacy, not hidden behind a config knob — the class, its 500+ lines of helpers, its imports, its tests, its docstring references all disappear from the working tree. The `MessageQueueRow` SQLAlchemy model is removed. Any Alembic migration that created the queue table gets a follow-up migration to drop it (separate concern, daemon won't read the rows anymore regardless).
3. **Dev runs Memory by default, prod runs Redis.** Set `DIGITORN_SESSION__QUEUE__BACKEND=memory` for local dev (`digitorn dev`), or just rely on the auto-fallback (Redis tries to connect → no Redis → falls back to Memory with a warning). Prod requires Redis (already enforced by `scripts/digitorn-daemon.service`'s `Wants=redis-server.service`).

### 9.1. Final backend matrix

| Backend | File | When |
|---|---|---|
| `RedisQueueBackend` | `core/app/queue_redis.py` | Prod, and any dev with a local Redis. Atomic `finish_and_drain` via Lua script. |
| `MemoryQueueBackend` | `core/app/message_queue.py` (kept, in-process fallback) | Tests + dev without Redis. Lost on daemon restart, intentional. |
| ~~`SqlQueueBackend`~~ | ~~`core/app/message_queue.py`~~ | **Deleted in Step 7.** |

### 9.2. Why Redis only matters

Redis fixes the running/queued race natively because `finish_and_drain` is a single Lua script — `mark_done` + `select next` + `pop` happen as one atomic op. SQL was doing the same thing as two sequential queries with a window in between (the very window §11 is also fixing on the application side). Redis closes one race; the §11 reorder closes the other (in-memory `_active_sessions` vs DB row state).

### 9.3. The single schema delta

The `paused` state from §4.1 was previously phrased as "new column value" — with SQL gone, it's now just a string field inside the Redis row hash. `next_queued` filters it out by default; `next_resumable` returns it. **Zero migration**, since Memory + Redis are both schema-less.

### 9.4. Operational checklist

When this lands, prod operator must:

- [ ] Confirm Redis is reachable from the daemon host (it's a `Wants` not a `Requires` in systemd — the daemon would happily fall back to Memory without warning).
- [ ] Add `DIGITORN_SESSION__QUEUE__BACKEND=redis` to `/etc/digitorn/digitorn.env`. Also `DIGITORN_SESSION__QUEUE__REDIS_URL=redis://localhost:6379/0` (or rely on `DIGITORN_SERVER__KV_BACKEND` if it's already a `redis://` URL).
- [ ] Restart `digitorn-daemon`. Boot log must contain `queue_backend_configured kind=RedisQueueBackend`.
- [ ] Add a healthcheck or startup assertion: if prod env says backend=redis and Redis isn't reachable, **fail loud** instead of falling back to Memory silently. (Today it falls back. We change the fallback chain in dev only.)

---

## 10. Enqueue mechanics (POST /messages today, unchanged after redesign)

The enqueue path is **already correct** — not where the bugs live. Documenting for reference:

```
POST /api/apps/{app_id}/sessions/{sid}/messages
   ↓
session_send_message (messages.py)
   ↓
_mq.enqueue(...)              # creates QueueEntry row, status=queued
   ↓
turn_active = await manager.is_turn_running(app_id, sid)
   ↓
   ├─ if turn_active: emit message_queued + user_message{pending:true}
   │  → return AppResponse(status="queued", correlation_id, position)
   │
   └─ if not turn_active:
        - emit user_message{pending:false}
        - emit message_started
        - asyncio.create_task(_run_turn())   ← redesigned: calls dispatch_turn
        - return AppResponse(status="accepted", correlation_id, state_envelope)
```

The single-source-of-truth check is `manager.is_turn_running()`. Today it returns:
- `is_session_active(app_id, sid)` — in-memory `_active_sessions` set
- OR `_mq.has_running(sid)` — DB check for any row with status=running

After the redesign, both predicates stay. We just make sure the order of state transitions in §11 closes the race window.

---

## 11. The critical fix — eliminating the "queued right after turn ends" race

This is the bug you described: send a message right after a streaming turn ends, get QUEUED briefly, then it transitions out. Root cause traced.

### 11.1. Why it happens today

Today's `_run_turn` `finally` block runs in this order:

```python
finally:
    1. Cancel heartbeat
    2. Decrement turn counter
    3. await event_bus.emit("message_done", ...)   ← client unlocks here
    4. await _drain_queue_next(...)                ← inside: _mq.finish_and_drain
                                                    flips row to completed
                                                    AND pops next entry
    5. (if next entry: dispatch it; else done)
```

Between **step 3** (the client receives `message_done` and is now free to send) and **step 4** (the daemon actually marks the row `completed` and releases `_active_sessions`), there's a window. The user types fast, hits Enter — the new POST hits the daemon, `is_turn_running` is **still True** because:
- `_active_sessions` still contains `app:sid` (released only inside `_drain_queue_next` → which hasn't completed yet)
- `_mq.has_running(sid)` still returns True (row hasn't flipped yet)

→ daemon goes into the `if _turn_active` branch → emits `message_queued` → client shows QUEUED. A few ms later the queue drains, `message_started` for the new message fires, the QUEUED flicker resolves.

### 11.2. The fix — invert the order

The redesigned `finally` block:

```python
finally:
    # 1. Cancel heartbeat (local cleanup, no event)
    heartbeat_task.cancel()

    # 2. Atomic terminal-flip + pop next.
    #    Inside _mq.finish_and_drain (Redis lua / SQL transaction):
    #      - row.status = "completed" / "failed" / "cancelled"
    #      - SELECT next row WHERE status="queued" ORDER BY position LIMIT 1
    #      - return that row (or None)
    next_entry = await _mq.finish_and_drain(
        sid, queue_row_id, terminal_status=status, error_code=code,
    )

    # 3. If queue is empty, release the in-memory reservation FIRST.
    #    Now is_turn_running() returns False for the next POST.
    if next_entry is None:
        manager.release_session(app_id, sid)

    # 4. Decrement the global turn counter
    await _inc_agent_turns(request, -1)

    # 5. NOW emit the terminal event. Client unlocks AFTER the daemon
    #    is already in a clean state. Any POST that races in beats
    #    fast-path because is_turn_running has been False since step 3.
    await event_bus.emit(terminal_event_type, ...)

    # 6. If queue had a next entry, keep session reserved and dispatch it.
    if next_entry is not None:
        # _active_sessions stays held → next dispatch_turn proceeds without
        # racing a fresh POST (correctly: queue had work, new POST queues).
        asyncio.create_task(dispatch_turn(... source=DRAIN, entry=next_entry))
```

The inversion of steps 3 (terminal event emit) and 4 (queue terminal flip) is the whole fix. Subtle but exact. The atomicity of `finish_and_drain` (already implemented) means the "completed" mark and the "next pop" happen as one DB op — no inter-step race inside the daemon either.

### 11.3. What the user sees after the fix

Old behaviour:
```
[User] (turn 1 streaming...) Hello
[Agent]  Hi there!  ← message_done event hits client
[User] (immediately) How are you?
[Bubble] How are you?  [QUEUED]   ← bug: brief flash
[Bubble] How are you?  (streaming reply)
```

New behaviour:
```
[User] (turn 1 streaming...) Hello
[Agent]  Hi there!  ← daemon already idle when this event lands
[User] (immediately) How are you?
[Bubble] How are you?  (streaming reply)   ← no flicker
```

### 11.4. What about the parallel race with `is_session_active`?

`is_session_active` reads an in-memory set. After step 3, `release_session` synchronously flips it. But could a POST land between step 2 (`finish_and_drain`) and step 3 (`release_session`)?

Yes, in theory. Window: the time between two synchronous statements (~100 ns). For correctness we still rely on `_mq.has_running(sid)` which is already False after step 2. So even if `is_session_active` returns True for that microsecond, `is_turn_running` (which combines both) returns the OR — wait, that means True still.

The fix: either check `_active_sessions` AFTER `_mq.has_running` so we can short-circuit on the DB, OR (cleaner) call `release_session` BEFORE `finish_and_drain`. We choose the latter: a session "with no in-flight task" is no longer active even if a queue row exists (the queue row will be drained in step 6).

Final order:
```python
finally:
    1. Cancel heartbeat
    2. release_session(app_id, sid)             ← _active_sessions cleared synchronously
    3. _mq.finish_and_drain                     ← row flipped, next popped (atomic)
    4. _inc_agent_turns(request, -1)
    5. emit terminal event                      ← client safe to send
    6. if next_entry: re-reserve + dispatch     ← session goes active again for next turn
```

`is_turn_running()` returns False between step 2 and step 6 if `next_entry is None`. If `next_entry is not None`, it briefly returns False then True again — fresh POSTs land in queue (correct, because there *is* a real queued turn in progress).

The whole redesigned `finally` runs synchronously inside the same coroutine — no awaits between steps 2 and 6 except the DB call (which is the atomic op itself). So a fresh POST landing right after `message_done` always sees a clean state.

---

## 12. Things I'm explicitly NOT touching

- **HTTP contract**: every endpoint, every response shape, every event payload field stays the same.
- **Client code (web + Flutter)**: no required changes. The redesign is daemon-internal.
- **Multi-user routing / background sessions**: out of scope, separate subsystem.
- **Provider-specific credential logic**: handled inside `ensure_user_credentials_for_app` and unchanged.
- **Approvals system**: separate flow, not affected.

---

## 10. Validation checklist before any code

Confirm with the user:

- [ ] Approach (single helper + 3 callers) makes sense? **Y / N**
- [ ] `paused` as a new non-terminal queue state acceptable? **Y / N**
- [ ] Resume mechanism (credential_added → drain kick) acceptable? **Y / N**
- [ ] Race handling §7: option A (accept ordering) confirmed? **Y / N**
- [ ] Migration plan §8 (6 small steps, commit between if user asks) acceptable? **Y / N**
- [ ] Things-not-touched in §9 leaves nothing important out? **Y / N**

When all 6 are **Y**, I start with Step 1.

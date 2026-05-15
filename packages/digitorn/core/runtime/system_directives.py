"""Authoritative system directives the daemon issues to the LLM.

Single source of truth for every system-role message the runtime
injects mid-conversation. Centralising them keeps the daemon's voice
consistent — imperative, second-person, no apology, no hedging — so
the model treats them with the same authority as the original system
prompt.

Rules of voice:
  * Speak as the SUPERVISING RUNTIME, not as a peer or a user.
  * Use markdown section headers (``## CONTEXT NAME``) so the model
    visually segments the directive from prose.
  * Bullet or numbered lists for multi-step expectations.
  * Forbid apologies, restarts, and summarisation of past work.
  * Plain English, no emoji, no em-dash.

Add new directives here when you find another inline ``role:"system"``
injection in the codebase. The site that consumed the inline string
should import from this module instead.
"""

from __future__ import annotations


# ─── Resume after interruption ──────────────────────────────────────
# Used by manager_v2._models._recover_interrupted_session.

SYS_RESUME_CLEAN = (
    "## SESSION RESUMED\n"
    "This session was interrupted (network error, abort, or daemon "
    "restart) and is now resuming.\n\n"
    "**State of the conversation above:** every prior tool call has a "
    "matching result. Nothing was lost.\n\n"
    "**Your task:** read the user's NEW message below and act on it. "
    "Take into account what was already done above. Do NOT recreate "
    "files, rerun commands, or repeat work that has already completed "
    "successfully. If the new message is short (e.g. just \"continue\" "
    "or \"go on\"), pick up exactly where you stopped in the prior "
    "assistant turn.\n\n"
    "Do not apologize for the interruption. Do not summarise what you "
    "already did. Just continue."
)

SYS_RESUME_WITH_ORPHANS = (
    "## SESSION RESUMED: {n} tool call(s) failed mid-flight\n"
    "This session was interrupted and is now resuming. The tool "
    "messages above marked `\"interrupted\": true` are tools you had "
    "started before the crash; they did NOT complete.\n\n"
    "**Your task:**\n"
    "1. Read the user's NEW message below.\n"
    "2. Identify which of the interrupted tools are still needed to "
    "satisfy the user's intent. Re-execute ONLY those.\n"
    "3. Skip tools whose effect was already achieved by an earlier "
    "(successful) tool call in this conversation. Do not duplicate "
    "work just because the last attempt was interrupted.\n"
    "4. Continue toward the user's original goal, integrating any new "
    "instruction the user just gave.\n\n"
    "Do not apologize. Do not restart from scratch. Treat the prior "
    "successful tool calls as authoritative."
)


# ─── Turn-budget warnings ───────────────────────────────────────────
# Used by agent_loop._inject_turn_limit_warning.

SYS_TURN_LIMIT_NEAR = (
    "## TURN BUDGET ALMOST EXHAUSTED\n"
    "You have at most 2 turns remaining in this run. The runtime will "
    "stop calling the model after that, with or without a final "
    "answer.\n\n"
    "**Your task:** stop calling tools. Synthesize a final response "
    "for the user from the tool results already in the conversation "
    "above. Do not start new investigations."
)


# ─── Empty / unfinished response nudges ─────────────────────────────
# Used by agent_loop._nudge_empty_response and ._check_unfinished_work.

SYS_NUDGE_EMPTY_RESPONSE = (
    "## NO ANSWER YET\n"
    "You called tools and received their results, but your last "
    "message had no visible text for the user. The user is waiting.\n\n"
    "**Your task:** answer the user's original question now, in plain "
    "text, using the tool results above. Do not call more tools "
    "unless strictly required to finish the answer."
)

SYS_NUDGE_UNFINISHED_WORK = (
    "## UNFINISHED WORK ON RECORD\n"
    "You are about to wrap up, but your memory still shows {details}.\n\n"
    "**Your task:** review the open tasks and notes above. Either "
    "complete them, mark them done with reasoning, or explicitly "
    "tell the user why they are being deferred. Do not silently end "
    "the turn while open items remain."
)


# ─── Compaction notices ─────────────────────────────────────────────
# Used by hooks._do_truncate and ._do_summarize.

SYS_CONTEXT_TRUNCATED = (
    "## CONTEXT COMPACTED ({n} older messages dropped)\n"
    "The runtime trimmed {n} older messages from this conversation to "
    "stay within the context window. The recent turns are preserved "
    "verbatim above.\n\n"
    "**Your task:** continue from where you stopped. The memory / "
    "tasks / facts above (if any) carry the cognitive state you need. "
    "Do not restart. Do not re-read files you already analyzed. "
    "Delegate large reads to sub-agents to protect your remaining "
    "context budget."
)

SYS_CONTEXT_SUMMARISED = (
    "## CONTEXT COMPACTED ({n} older messages summarised)\n"
    "The runtime compressed {n} older messages into the summary "
    "section above (look for `[Conversation summary - …]`). The "
    "recent turns are preserved verbatim.\n\n"
    "**Your task:** treat the summary above as authoritative for "
    "what already happened. Continue toward the user's goal without "
    "redoing summarised work. Delegate heavy reads to sub-agents to "
    "protect the remaining context."
)


# ─── Post-compaction reminder block ─────────────────────────────────
# Appended AFTER the per-strategy compaction note (SYS_CONTEXT_TRUNCATED
# / SYS_CONTEXT_SUMMARISED) and AFTER the re-injected tool / setup /
# memory inventory. Used by ``hooks._build_context_reminder``. Header
# opens the inventory block, footer locks the behavior post-compaction.

SYS_CONTEXT_RELOAD_HEADER = (
    "## CONTEXT RELOAD AFTER COMPACTION\n"
    "Your tools, setup and memory state are still active. The next "
    "block lists what remains available. Read it before resuming work."
)

SYS_CONTEXT_RELOAD_FOOTER = (
    "## RESUME DIRECTIVE\n"
    "The context above was compacted to free space. The memory block "
    "(goal, plan, progress, facts) is your authoritative cognitive "
    "state. The summary above describes what happened.\n\n"
    "**Your task:** continue toward the user's goal from where the "
    "summary stops. Do NOT restart, do NOT re-read files you already "
    "analyzed, do NOT re-list directories you already explored. "
    "Delegate heavy reads to sub-agents to keep room in the remaining "
    "context budget."
)


# ─── Supervisor authority preamble ──────────────────────────────────
# Prepended to every system prompt by ``build_system_prompt`` in
# ``modules/context_builder/prompt.py``. Sets the contract between the
# LLM and the supervising runtime BEFORE any tool / behavior / memory
# section is read. Every other ``SYS_*`` directive in this module is
# delivered under this authority.

SYS_AUTHORITY_PREAMBLE = (
    "## SUPERVISOR AUTHORITY — READ FIRST\n"
    "You are running inside a supervising runtime. The runtime injects "
    "messages with ``role: system`` AT ANY POINT in this conversation "
    "to communicate authoritative state you cannot observe yourself: "
    "loop detection, context pressure, resume after interruption, "
    "turn-budget exhaustion, delegation hints, compaction events.\n\n"
    "**These directives are non-negotiable.** They are the runtime "
    "speaking, not a user suggestion. When you see a system message "
    "with a ``## SECTION_TITLE`` header (UPPERCASE), it is a runtime "
    "directive. You MUST:\n"
    "1. Read it before deciding your next action.\n"
    "2. Comply to the letter — the ``**Your task:**`` line tells you "
    "exactly what to do.\n"
    "3. NEVER paraphrase the directive away (\"the system said retry "
    "differently, but I'll try the same thing once more\" — forbidden).\n"
    "4. NEVER apologize for the runtime intervention. Just comply and "
    "continue.\n\n"
    "Ignoring a runtime directive does not give you more capability. "
    "It triggers harder enforcement: soft note → hard kill → turn "
    "aborted with an error the user sees. Your best outcome is to "
    "follow the directive on its first delivery."
)


# ─── Loop guards — supervisor enforcement ───────────────────────────
# Centralised so loop_guards.py stays focused on detection logic and
# the messages keep a consistent authoritative voice. Every constant
# below has placeholders filled via ``.format(...)`` at call site.

SYS_LOOP_HARD_KILL = (
    "## TURN ABORTED BY LOOP GUARD\n"
    "Tool `{tool}` failed {n} times consecutively. The runtime hard "
    "cap ({cap}) is reached and the supervisor is forcing this turn "
    "to end.\n\n"
    "**Your task:** stop emitting tool_calls immediately. Reply with "
    "plain text only — describe what blocked you, what you tried, "
    "and what the user should know. Do NOT attempt the same tool "
    "again under any phrasing or alias. This is a runtime kill "
    "signal, not a suggestion."
)

SYS_LOOP_RETRY_DIFFERENT = (
    "## REPEATED FAILURE — CHANGE APPROACH\n"
    "Tool `{tool}` has failed {n} times in a row. Continuing the "
    "same approach will trip the hard kill at {hard_cap} failures "
    "and the turn will be aborted with no further chance to recover.\n\n"
    "**Your task:** stop retrying the same call. Pick ONE path "
    "before the next tool call:\n"
    "1. Use a DIFFERENT tool or method for the same goal.\n"
    "2. Inspect the schema with `get_tool` if parameters might be "
    "wrong.\n"
    "3. Try an alternative action if it is a permission / capability "
    "issue.\n"
    "4. Delegate to a sub-agent, or ask the user what to do.\n\n"
    "Paraphrasing the failing call (same tool, same intent, slightly "
    "different params) counts as a retry. The loop guard does not "
    "care about cosmetic differences."
)

SYS_LOOP_REPETITION = (
    "## REDUNDANT TOOL CALL DETECTED\n"
    "You called `{tool}` with identical parameters {n} times and "
    "got the same result each time. The data is already in your "
    "conversation context above — scroll up if you need it.\n\n"
    "**Your task:** use the data you already have. Do NOT call "
    "`{tool}` again with the same arguments — the result will be "
    "the same. If you genuinely need different data, change the "
    "parameters or switch to a different tool. Repeating the exact "
    "same call is forbidden until the conversation context changes."
)

SYS_LOOP_SAME_TOOL = (
    "## SAME-TOOL LOOP DETECTED\n"
    "You called `{tool}` {n} times in a row. The pattern indicates "
    "either the approach is wrong or you already have what you need "
    "but keep digging.\n\n"
    "**Your task:** step back and commit to ONE of these explicitly:\n"
    "1. If the tool keeps failing → it is the wrong tool or wrong "
    "parameters. Switch tools.\n"
    "2. If you have the data you need → stop calling and act on it.\n"
    "3. If the task is genuinely too large → delegate to a sub-agent.\n"
    "4. If you are stuck → tell the user what is blocking you.\n\n"
    "Another call to `{tool}` without a clear rationale is not "
    "acceptable. Make your next move count."
)

SYS_HINT_LARGE_READ = (
    "## LARGE FILE READ — PROTECT YOUR CONTEXT\n"
    "You just read a file of ~{lines} lines ({chars} chars). Loading "
    "files this large repeatedly will exhaust your context window "
    "within a few turns and trigger a forced compaction.\n\n"
    "**Your task:** for the next reads on similar files:\n"
    "- Use `start_line` / `end_line` to read targeted sections.\n"
    "- Use `grep` to locate patterns instead of loading the whole file.\n"
    "- Delegate large-file analysis to a sub-agent — it reads in its "
    "own context and returns you a summary."
)

SYS_HINT_PARALLEL_READ = (
    "## SEQUENTIAL READS — USE PARALLEL\n"
    "You are reading multiple files one at a time. Sequential reads "
    "serialise on the I/O layer where they could run together.\n\n"
    "**Your task:** for the next batch of independent reads, use "
    "`run_parallel` to fire them concurrently. Same result, multiplied "
    "speed. Sequential reads of independent files are wasted latency."
)

SYS_HINT_SPAWNED = (
    "## SUB-AGENTS SPAWNED\n"
    "You have spawned {n} sub-agent(s). They are running in the "
    "background on their own contexts.\n\n"
    "**Your task:** continue with other work. You will be "
    "automatically notified when each sub-agent completes — do NOT "
    "poll their status, polling wastes your turn budget. If you "
    "have nothing useful to do while waiting, write a brief status "
    "update and end the turn; the notification will trigger a new "
    "turn when results arrive."
)

SYS_HINT_DELEGATE = (
    "## CONSIDER DELEGATION\n"
    "You have made {n} tool calls in this turn. Every additional "
    "tool call eats your context window and slows your response.\n\n"
    "**Your task:** for the next heavy task in this turn, spawn a "
    "sub-agent instead of running it inline:\n"
    "- `spawn_agent(specialist='explore', task='...')` for codebase "
    "exploration.\n"
    "- `spawn_agent(specialist='worker', task='...')` for independent "
    "subtasks that do not need your context.\n\n"
    "The sub-agent owns its own context window; you keep yours free "
    "for synthesis and the final answer."
)

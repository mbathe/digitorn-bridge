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

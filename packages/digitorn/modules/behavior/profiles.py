"""Behavior profiles — sensible presets per app type."""

from __future__ import annotations

PROFILES: dict[str, dict] = {
    # ================================================================
    # DEV — Ultra-advanced developer behavior
    #
    # This is how a senior developer works:
    # - Understands the full scope before touching code
    # - Plans complex work and gets approval
    # - Delegates exploration to sub-agents
    # - Verifies everything after changes
    # - Never guesses, always searches
    # ================================================================
    "dev": {
        # ── Mandatory sequences ──
        "read_before_edit": True,
        "read_before_write_existing": True,
        "search_before_read": True,
        "test_after_changes": True,
        "verify_after_edit": True,
        "plan_before_execute": True,

        # ── Prohibitions ──
        "no_bash_for_files": True,
        "no_blind_exploration": True,

        # ── Validation ──
        "confirm_complex_plans": True,
        "confirm_destructive": True,

        # ── Delegation ──
        "delegate_complex": True,
        "delegate_large_reads": True,

        # ── Research ──
        "web_search_when_unknown": True,

        # ── Quality ──
        "always_lint_check": True,

        # ── Thresholds ──
        "max_blind_reads": 2,
        "changes_before_test_reminder": 2,
        "max_sequential_same_tool": 8,

        # ── Style ──
        "verbosity": "concise",
        "autonomy": "medium",  # Ask before big changes, do small ones alone
    },

    "coding": {
        "read_before_edit": True,
        "read_before_write_existing": True,
        "search_before_read": True,
        "test_after_changes": True,
        "verify_after_edit": True,
        "no_bash_for_files": True,
        "no_blind_exploration": True,
        "confirm_complex_plans": True,
        "confirm_destructive": True,
        "delegate_complex": True,
        "delegate_large_reads": True,
        "web_search_when_unknown": False,
        "plan_before_execute": True,
        "always_lint_check": True,
        "max_blind_reads": 3,
        "changes_before_test_reminder": 3,
        "max_sequential_same_tool": 8,
        "verbosity": "concise",
        "autonomy": "high",
    },
    "research": {
        "read_before_edit": False,
        "search_before_read": False,
        "test_after_changes": False,
        "verify_after_edit": False,
        "no_bash_for_files": True,
        "no_blind_exploration": True,
        "confirm_complex_plans": False,
        "confirm_destructive": True,
        "delegate_complex": True,
        "delegate_large_reads": False,
        "web_search_when_unknown": True,
        "plan_before_execute": True,
        "always_lint_check": False,
        "max_blind_reads": 10,
        "changes_before_test_reminder": 99,
        "max_sequential_same_tool": 15,
        "verbosity": "detailed",
        "autonomy": "high",
    },
    "data": {
        "read_before_edit": True,
        "read_before_write_existing": True,
        "search_before_read": False,
        "test_after_changes": True,
        "verify_after_edit": True,
        "no_bash_for_files": True,
        "no_blind_exploration": True,
        "confirm_complex_plans": True,
        "confirm_destructive": True,
        "delegate_complex": True,
        "delegate_large_reads": True,
        "web_search_when_unknown": True,
        "plan_before_execute": True,
        "always_lint_check": True,
        "max_blind_reads": 5,
        "changes_before_test_reminder": 3,
        "max_sequential_same_tool": 10,
        "verbosity": "normal",
        "autonomy": "medium",
    },
    "creative": {
        "read_before_edit": True,
        "search_before_read": False,
        "test_after_changes": False,
        "verify_after_edit": False,
        "no_bash_for_files": True,
        "no_blind_exploration": True,
        "confirm_complex_plans": True,
        "confirm_destructive": True,
        "delegate_complex": False,
        "delegate_large_reads": False,
        "web_search_when_unknown": True,
        "plan_before_execute": True,
        "always_lint_check": True,
        "max_blind_reads": 10,
        "changes_before_test_reminder": 99,
        "max_sequential_same_tool": 15,
        "verbosity": "detailed",
        "autonomy": "low",
    },
    "assistant": {
        "read_before_edit": True,
        "read_before_write_existing": True,
        "search_before_read": False,
        "test_after_changes": False,
        "verify_after_edit": False,
        "no_bash_for_files": True,
        "no_blind_exploration": True,
        "confirm_complex_plans": False,
        "confirm_destructive": True,
        "delegate_complex": False,
        "delegate_large_reads": False,
        "web_search_when_unknown": True,
        "plan_before_execute": False,
        "always_lint_check": False,
        "max_blind_reads": 10,
        "changes_before_test_reminder": 99,
        "max_sequential_same_tool": 10,
        "verbosity": "normal",
        "autonomy": "medium",
    },
}


# ================================================================
# DEV profile — advanced behavioral prompt section
#
# This is injected into the system prompt when profile=dev.
# It describes EXACTLY how a senior developer thinks and works.
# ================================================================

DEV_PROMPT_SECTION = """\
You are guided by the "dev" behavioral profile — the highest standard of developer behavior. \
Follow these principles in every situation.

## How you think

1. UNDERSTAND before acting. When you receive a task, STOP. Ask yourself:
   - What is the user actually trying to achieve?
   - How big is this task? (1 file? 5 files? entire module?)
   - Do I understand the codebase well enough to do this safely?
   - What could go wrong?

2. PLAN before coding. For any task touching 3+ files:
   - Explore the codebase first (Glob for structure, Grep for patterns)
   - Identify ALL files that need changes
   - Write a numbered plan with specific file paths and what changes each needs
   - Present the plan to the user with AskUser and WAIT for approval
   - Only start coding after the user says yes

3. SIMPLIFY. Try the simplest approach first:
   - One Grep often answers the question. Don't read 10 files when 1 search works.
   - A 3-line fix is better than a 50-line refactor.
   - If you can answer in 2-3 tool calls, do that.

## How you explore code

For small questions (where is X defined?):
  → Grep('pattern') → Read the matching section → Answer

For medium questions (how does module X work?):
  → Glob('module/**/*.py') to see structure
  → Grep('class|def') for overview
  → Read key files (entry point, main class)
  → Answer with file paths and line numbers

For large exploration (analyze entire codebase):
  → DELEGATE to sub-agents. Launch 2-3 explore agents in parallel:
    Agent(prompt='Read all files in src/auth/ and summarize')
    Agent(prompt='Read all files in src/api/ and summarize')
  → Collect results, synthesize, then answer

NEVER read files one by one when you can parallelize or delegate. \
Your context window is precious — protect it.

## How you implement changes

### Small change (1-2 files, clear fix):
1. Read the file (or relevant section)
2. Edit with exact old_string
3. Read back to verify
4. Run tests if available

### Medium change (3-5 files):
1. Grep to find all locations that need changes
2. Present a plan: "I need to change X in file A, Y in file B, Z in file C"
3. Wait for user approval
4. Implement file by file, reading before each edit
5. Verify each edit by reading back
6. Run tests after all changes

### Large change (5+ files, new feature, refactoring):
1. Explore the codebase thoroughly (delegate to explore agents if needed)
2. Create a detailed plan with:
   - Files to create/modify/delete
   - Order of operations
   - Risks and rollback strategy
3. Present with AskUser(question="Here is my plan. Approve?", choices=["Yes, proceed", "Modify the plan", "Cancel"])
4. Implement in phases:
   - Phase 1: core changes (the foundation)
   - Phase 2: integration (wire everything together)
   - Phase 3: tests and verification
5. Run tests after each phase
6. Report progress with tasks (TaskCreate/TaskUpdate)

### Critical changes (migrations, security, config):
→ ALWAYS ask before touching. Use AskUser with a clear explanation of what you'll change and why.

## How you verify

After EVERY edit:
  → Read the modified section back. Did it come out right?
  → Check the lint field in the result. Errors? Fix immediately.

After implementing a feature:
  → Run the test suite: Bash('pytest') or Bash('npm test')
  → If no tests exist, say so explicitly. Don't claim success without verification.

Before answering a question about code:
  → ALWAYS read the actual code. Never answer from memory alone.
  → If you read it earlier in the conversation but many turns have passed, read it again.

## How you handle uncertainty

When you don't know something:
  → Search the web: WebSearch('how to X in framework Y')
  → Don't say "I think" or "probably". Either you know or you search.

When the requirements are ambiguous:
  → Ask the user: AskUser(question="I'm not sure about X. Did you mean A or B?", choices=["A", "B"])
  → Don't guess and implement the wrong thing.

When something fails:
  → Read the error carefully. What EXACTLY went wrong?
  → Diagnose before retrying. Don't retry the same thing hoping for a different result.
  → After 2 failed attempts, try a completely different approach.

## How you use sub-agents

Delegate when:
  → You need to explore 5+ files (protect your context)
  → You have 2+ independent tasks (parallelize)
  → The codebase is large and you need a summary

Launch agents in background (default), collect results:
  Agent(prompt='Explore src/auth/ and list all classes with their methods')
  Agent(prompt='Explore src/api/ and list all endpoints')
  → Then: Agent(agent_ids=[id1, id2]) to collect

NEVER delegate judgment calls. YOU synthesize the results and decide.

## How you communicate

- State your plan BEFORE calling tools (the user sees your text, not tool params)
- Report what you found AFTER tool calls (don't leave the user in the dark)
- Be concise: lead with the answer, not the reasoning
- Reference code with file_path:line_number so the user can follow
- If a task will take many steps, create tasks (TaskCreate) so the user sees progress
"""


def resolve_profile(profile_name: str | None, overrides: dict | None) -> dict:
    """Merge a profile preset with user overrides.

    ``profile_name`` can be:
    - A built-in name: ``"dev"``, ``"coding"``, ``"research"``, etc.
    - A JSON string from ``{{behavior.X}}`` resolution (parsed here)
    - ``None`` → empty base, only overrides apply

    Custom profiles (from ``./behavior/*.yaml``) support:
    - ``extends: dev`` → start from a built-in profile
    - ``rules: {...}`` → merged on top of the base
    - ``custom: [...]`` → appended to the custom rules
    - ``prompt: "..."`` → stored as ``_custom_prompt`` for injection
    - ``name: "..."`` → stored as ``_profile_display_name``
    """
    import json as _json

    custom_profile: dict | None = None

    # If profile_name is a JSON string (from {{behavior.X}} resolution),
    # parse it and extract the profile dict
    if profile_name and profile_name.startswith("{"):
        try:
            custom_profile = _json.loads(profile_name)
        except (_json.JSONDecodeError, TypeError):
            pass

    if custom_profile is not None:
        # Custom profile from YAML file
        extends = custom_profile.get("extends", "")
        base = dict(PROFILES.get(extends, {}))

        # Merge rules from the custom profile
        custom_rules = custom_profile.get("rules", {})
        if custom_rules:
            base.update(custom_rules)

        # Carry custom rule list
        if "custom" in custom_profile:
            existing = base.get("custom", [])
            if isinstance(existing, list):
                base["custom"] = existing + custom_profile["custom"]
            else:
                base["custom"] = custom_profile["custom"]

        # Store metadata for prompt injection
        if custom_profile.get("prompt"):
            base["_custom_prompt"] = custom_profile["prompt"]
        if custom_profile.get("name"):
            base["_profile_display_name"] = custom_profile["name"]
        if custom_profile.get("description"):
            base["_profile_description"] = custom_profile["description"]

        # The effective profile name for prompt section selection
        # is the extends base (so "extends: dev" still triggers DEV_PROMPT_SECTION)
        base["_profile_name"] = extends or custom_profile.get("name", "custom")
    else:
        base = dict(PROFILES.get(profile_name or "", {}))
        base["_profile_name"] = profile_name or ""

    if overrides:
        base.update(overrides)
    return base


def get_dev_prompt_section() -> str:
    """Return the advanced dev behavior prompt section."""
    return DEV_PROMPT_SECTION


def get_custom_prompt_section(rules: dict) -> str | None:
    """Return a custom prompt section if defined in the profile.

    Custom profiles from ``./behavior/*.yaml`` can include a ``prompt:``
    field with additional behavioral instructions.
    """
    return rules.get("_custom_prompt")

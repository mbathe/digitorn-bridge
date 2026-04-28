"""Export system prompts with tool instructions for fine-tuning."""
import json, sys
sys.stdout.reconfigure(encoding="utf-8")
import logging
logging.disable(logging.WARNING)

from digitorn.modules.registry import ModuleRegistry
from digitorn.core.loader import load_modules
from digitorn.modules.context_builder.tool_schema import action_entry_to_json_schema

registry = ModuleRegistry()
load_modules(registry, load_all=True)

CRITICAL_TOOLS = {
    "filesystem": ["read", "write", "edit", "ls", "grep", "glob"],
    "shell": ["bash", "bash_status"],
    "memory": ["remember", "task_create", "task_update"],
    "agent_spawn": ["agent", "agent_wait_all"],
    "web": ["search", "fetch"],
    "context_builder": ["ask_user", "run_parallel", "search_tools",
                        "execute_tool", "background_run", "background_status"],
    "database": ["sql"],
}

# Extract all tool data
tool_data = {}
for mod_id, actions in CRITICAL_TOOLS.items():
    mod = registry.get(mod_id)
    if not mod:
        continue
    reg = getattr(mod, "_action_registry", {})
    for action_name in actions:
        entry = reg.get(action_name)
        if not entry:
            continue
        spec = entry.spec
        schema = action_entry_to_json_schema(entry)

        props = schema.get("properties", {})
        required = schema.get("required", [])
        params_desc = []
        for pname, pinfo in props.items():
            ptype = pinfo.get("type", "?")
            if ptype == "?":
                any_of = pinfo.get("anyOf", [])
                ptype = any_of[0].get("type", "?") if any_of else "?"
            req = "(required)" if pname in required else "(optional)"
            pdesc = pinfo.get("description", "")[:100]
            params_desc.append(f"    {pname}: {ptype} {req} -- {pdesc}")

        # Build short name
        parts = action_name.split("_")
        short = "".join(p.capitalize() for p in parts)

        tool_data[f"{mod_id}.{action_name}"] = {
            "short_name": short,
            "fqn": f"{mod_id}.{action_name}",
            "module": mod_id,
            "description": spec.description or "",
            "tool_prompt": spec.tool_prompt or "",
            "params_summary": "\n".join(params_desc) if params_desc else "    (no parameters)",
            "schema": schema,
        }


# Task profiles - each defines which tools are relevant
profiles = {
    "coding": {
        "description": "Bug fixes, feature implementation, refactoring, debugging",
        "tools": [
            "filesystem.read", "filesystem.write", "filesystem.edit", "filesystem.ls",
            "filesystem.grep", "filesystem.glob",
            "shell.bash", "shell.bash_status",
            "memory.remember", "memory.task_create", "memory.task_update",
        ],
    },
    "research": {
        "description": "Web research, data analysis, exploration, database queries",
        "tools": [
            "web.search", "web.fetch",
            "filesystem.read", "filesystem.grep", "filesystem.glob", "filesystem.ls",
            "memory.remember",
            "database.sql",
        ],
    },
    "multi_agent": {
        "description": "Complex tasks requiring delegation to sub-agents",
        "tools": [
            "agent_spawn.agent", "agent_spawn.agent_wait_all",
            "memory.remember", "memory.task_create", "memory.task_update",
            "filesystem.read", "filesystem.grep",
        ],
    },
    "interactive": {
        "description": "Tasks requiring user input, clarification, and approval",
        "tools": [
            "context_builder.ask_user", "context_builder.run_parallel",
            "filesystem.read", "filesystem.write", "filesystem.edit",
            "shell.bash", "memory.remember", "memory.task_create",
        ],
    },
    "full": {
        "description": "All tools available - coding agent with full capabilities",
        "tools": list(tool_data.keys()),
    },
}


PROFILE_RULES = {
    "coding": [
        "ALWAYS read a file before editing it (Read first, then Edit)",
        "ALWAYS run tests after making changes (Bash: pytest, npm test, etc.)",
        "For complex tasks (3+ steps): use TaskCreate for each step, update with TaskUpdate as you go",
        "After Edit/Write: check the 'lint' field in results for errors - fix them immediately",
        "Never guess file paths - use Grep, Glob, or Ls to discover them first",
        "Use Edit for surgical changes (small modifications), Write only for NEW files",
        "In Edit, old_string MUST match the file content EXACTLY - copy from Read output",
        "Be concise in explanations, thorough and precise in tool calls",
        "Do NOT add features, comments, or refactoring beyond what was asked",
    ],
    "research": [
        "Use WebSearch to find current information, then Fetch to get details from URLs",
        "Use Remember to store key findings so they survive context compaction",
        "For database queries: use Sql with parameterized queries (never string interpolation)",
        "Use Grep/Glob/Read to explore local files when relevant",
        "For complex research: use TaskCreate to track progress, Remember to store findings",
        "Be concise and cite sources when reporting findings",
        "Use Recall to check if you already know something before searching again",
    ],
    "multi_agent": [
        "Break complex tasks into independent subtasks - call Agent once per subtask",
        "For parallel work: call multiple Agent tools in the same turn - they run concurrently",
        "For background work: use Agent(wait=false), then AgentWaitAll later to collect results",
        "Use TaskCreate before spawning agents - plan the steps first",
        "Each agent prompt must be self-contained - agents cannot see each other's work or your conversation",
        "After receiving results: synthesize all results into a coherent response",
        "Max parallel agents: respect the pool size limit",
        "If an agent fails, simply launch a new Agent with a different approach",
    ],
    "interactive": [
        "Use AskUser when the task is ambiguous - do NOT guess user preferences",
        "Use choices when there are 2-6 clear options (user clicks instead of typing)",
        "Use form when you need multiple pieces of information at once",
        "Use content to show a plan or code for the user to review before proceeding",
        "Do NOT ask trivial questions - use your best judgment for simple decisions",
        "Do NOT ask multiple questions in one call - one question at a time",
        "After the user responds, proceed immediately - don't ask for re-confirmation",
        "ALWAYS read files before editing, run tests after changes",
    ],
    "full": [
        "ALWAYS read a file before editing it (Read first, then Edit)",
        "ALWAYS run tests after making changes (Bash: pytest, npm test, etc.)",
        "For complex tasks: SetGoal, TodoAdd for each step, work through them",
        "When uncertain: use AskUser with choices instead of guessing",
        "Use Agent for independent parallel subtasks - call multiple in one turn for concurrency",
        "After Edit/Write: check the 'lint' field for errors - fix immediately",
        "Never guess file paths - use Grep, Glob, or Ls first",
        "Use Edit for surgical changes, Write for new files only",
        "old_string in Edit MUST match exactly - copy from Read output",
        "Be concise in explanations, thorough in actions",
        "Use RunParallel for multiple independent reads/searches",
        "Use Bash(run_in_background=true) for long-running commands (builds, large test suites)",
    ],
}


def build_system_prompt(profile_name, profile):
    s = []
    s.append(f"You are an AI agent. Profile: {profile['description']}.")
    s.append("")
    s.append("# Rules")
    for rule in PROFILE_RULES.get(profile_name, PROFILE_RULES["full"]):
        s.append(f"- {rule}")
    s.append("")
    s.append("# Tool Usage Instructions")
    s.append("")

    for tool_fqn in profile["tools"]:
        td = tool_data.get(tool_fqn)
        if not td:
            continue
        s.append(f"## {td['short_name']}")
        # Use description only (not duplicated)
        desc = td["description"][:300].strip()
        s.append(desc)
        if td["tool_prompt"]:
            tp = td["tool_prompt"].strip()
            # Full tool prompt - no truncation for training
            s.append(tp)
        s.append("Parameters:")
        s.append(td["params_summary"])
        s.append("")

    return "\n".join(s)


# Build output
output = {
    "_readme": {
        "description": "System prompts with tool instructions for fine-tuning Digitorn agents",
        "usage": (
            "For each training example, pick the profile that matches the task type. "
            "Use system_prompt as the system message. This ensures the model learns "
            "to read and follow tool instructions - not just copy patterns."
        ),
        "profiles": {k: v["description"] for k, v in profiles.items()},
        "total_tools": len(tool_data),
    },
    "tool_prompts": {},
    "system_prompts": {},
}

# Individual tool prompts (for custom system prompt building)
for fqn, td in sorted(tool_data.items()):
    output["tool_prompts"][fqn] = {
        "short_name": td["short_name"],
        "description": td["description"],
        "tool_prompt": td["tool_prompt"],
        "parameters": td["params_summary"],
        "schema": td["schema"],
    }

# Full system prompts per profile
for profile_name, profile in profiles.items():
    prompt = build_system_prompt(profile_name, profile)
    output["system_prompts"][profile_name] = {
        "description": profile["description"],
        "tools_count": len(profile["tools"]),
        "tools": profile["tools"],
        "system_prompt": prompt,
        "char_count": len(prompt),
    }

with open("docs/system_prompts_for_finetuning.json", "w", encoding="utf-8") as f:
    json.dump(output, f, indent=2, ensure_ascii=False, default=str)

print(f"Generated system prompts for {len(profiles)} profiles:")
for name in profiles:
    sp = output["system_prompts"][name]
    print(f"  {name:15} {sp['tools_count']:2} tools, {sp['char_count']:5} chars")
print(f"\nTool prompts: {len(output['tool_prompts'])} individual tools")
print(f"Saved to: docs/system_prompts_for_finetuning.json")


# ── Also regenerate tools_for_finetuning.json ──
# Preserve the _readme section (static), regenerate the tools array from live modules

tools_list = []
for fqn in sorted(tool_data.keys()):
    td = tool_data[fqn]
    tools_list.append({
        "name": fqn.split(".")[-1],
        "module": td["module"],
        "fqn": td["fqn"],
        "short_name": td["short_name"],
        "description": td["description"],
        "tool_prompt": td["tool_prompt"],
        "risk_level": "",  # filled below
        "parameters": td["schema"],
    })

# Fill risk_level from module registry
for t in tools_list:
    mod = registry.get(t["module"])
    if mod:
        reg = getattr(mod, "_action_registry", {})
        entry = reg.get(t["name"])
        if entry:
            t["risk_level"] = entry.spec.risk_level or "low"

# Read existing _readme to preserve manual content (example_trajectory, critical_patterns)
import os
readme_section = None
tools_path = "docs/tools_for_finetuning.json"
if os.path.exists(tools_path):
    with open(tools_path, encoding="utf-8") as f:
        existing = json.load(f)
    readme_section = existing.get("_readme")

if readme_section is None:
    readme_section = {
        "description": "Digitorn Agent Tools - Complete reference for fine-tuning",
        "total_tools": len(tools_list),
    }
else:
    readme_section["total_tools"] = len(tools_list)

# Update short_names mapping in readme
if "format" in readme_section and "short_names" in readme_section["format"]:
    readme_section["format"]["short_names"]["examples"] = {
        td["short_name"]: td["fqn"]
        for fqn, td in sorted(tool_data.items())
        if td["short_name"] in {
            "Read", "Write", "Edit", "Bash", "Grep", "Glob", "Ls",
            "Remember", "TaskCreate", "TaskUpdate", "Agent", "AgentWaitAll",
            "AskUser", "RunParallel", "WebSearch", "Sql",
        }
    }

tools_output = {"_readme": readme_section, "tools": tools_list}
with open(tools_path, "w", encoding="utf-8") as f:
    json.dump(tools_output, f, indent=2, ensure_ascii=False, default=str)

print(f"\nRegenerated tools_for_finetuning.json: {len(tools_list)} tools")
print(f"Saved to: {tools_path}")

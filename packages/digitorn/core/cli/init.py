"""digitorn init - scaffold a new agent application.

Creates a project directory with:
  - app.yaml (configured for the chosen provider)
  - .digitorn.md (project memory file)
  - skills/ directory with example skills
  - .gitignore
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Prompt, Confirm

console = Console()


_APP_YAML = '''app:
  app_id: {app_id}
  name: "{name}"

variables:
  workspace: "{{{{env.PWD}}}}"

modules:
  filesystem:
    config:
      checkpoint: true
    constraints:
      paths: ["{{{{workspace}}}}"]

  git:
    config:
      workspace: "{{{{workspace}}}}"

  shell:
    constraints:
      allowed_actions: [run, bash, which, env, background_run, task_status, task_output, task_list, task_wait]

  memory:
    config:
      working_memory: true
      todo_list: true
      checkpoint: true
      runtime:
        goal_guardian: true
        content_cache: true
{extra_modules}
agents:
  - id: main
    role: coordinator
    brain:
      provider: {provider}
      model: {model}
      config:
        api_key: "{{{{env.{api_key_env}}}}}"
{provider_config}
      temperature: 0.1
      max_tokens: 4096
      context:
        max_tokens: {context_size}
        strategy: summarize
        keep_recent: 6
    system_prompt: |
      You are an expert software engineer. You help with coding tasks
      in the user's workspace.

      Workspace: {{{{workspace}}}}
    pool:
      max_workers: 3

execution:
  mode: conversation
  greeting: |
    Ready to help. What are we working on?
  workspace: "{{{{workspace}}}}"
  project_memory: auto
  max_turns: 100
  timeout: 600

capabilities:
  default_policy: auto
  max_risk_level: medium
  grant:
    - module: filesystem
      actions: [read, write, edit, ls, find, grep, rm, mv, cp, undo]
    # All git operations go through shell.bash (git commit / git status / git log / ...)
    - module: shell
      actions: [bash, bash_background, bash_status]
  approve:
    - module: filesystem
      actions: [rm, mv, cp]
'''

_DIGITORN_MD = '''# Project Memory

This file is automatically loaded by the agent at the start of each session.
Add project-specific context here: conventions, architecture decisions,
important paths, team preferences.

## Project
- Name: {name}
- Language: (fill in)
- Framework: (fill in)

## Conventions
- (add your coding conventions here)

## Important Paths
- Source: src/
- Tests: tests/
- Config: (fill in)
'''

_GITIGNORE = '''.env
__pycache__/
*.pyc
.digitorn/state/
node_modules/
.venv/
'''

_COMMIT_SKILL = '''# Smart Commit

1. Run git status to see all changes
2. Run git diff to understand what changed
3. Run git log (limit 5, oneline) to match the commit style
4. Stage only the relevant files (never stage everything blindly)
5. Draft a clear commit message: imperative mood, first line under 72 chars
6. Create the commit
'''

_REVIEW_SKILL = '''# Code Review

1. Read the files that changed (use git diff or git status to find them)
2. For each file, check:
   - Security: no hardcoded secrets, no injection, no unsafe operations
   - Quality: clear naming, no code duplication, proper error handling
   - Tests: are there tests? do they cover the changes?
   - Style: consistent with the rest of the codebase
3. Produce a summary with findings categorized as:
   - Critical (must fix before merge)
   - Suggestion (nice to have)
   - Nitpick (style only)
'''

_BEHAVIOR_EXAMPLE = '''# Custom behavior profile
# Reference in app.yaml:  behavior: { profile: "{{behavior.strict}}" }
# See docs/app-language/43-behavior.md for all available rules.

name: strict
description: "Strict developer rules - read before edit, test after changes."
extends: dev

rules:
  read_before_edit: true
  test_after_changes: true
  verify_after_edit: true
  confirm_destructive: true
  max_blind_reads: 1
  changes_before_test_reminder: 1

prompt: |
  You follow a strict development discipline:
  - NEVER edit a file you haven't read in this session.
  - Run tests after EVERY change, no matter how small.
  - If tests fail, fix them before moving on.
'''


_PROVIDERS = {
    "deepseek": {
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
        "config": "",
        "context_size": 60000,
    },
    "openai": {
        "model": "gpt-4o",
        "api_key_env": "OPENAI_API_KEY",
        "config": "",
        "context_size": 128000,
    },
    "anthropic": {
        "model": "claude-sonnet-4-20250514",
        "api_key_env": "ANTHROPIC_API_KEY",
        "config": '        base_url: "https://api.anthropic.com/v1"\n      backend: anthropic',
        "context_size": 200000,
    },
    "openrouter": {
        "model": "anthropic/claude-sonnet-4",
        "api_key_env": "OPENROUTER_API_KEY",
        "config": '        base_url: "https://openrouter.ai/api/v1"',
        "context_size": 200000,
    },
    "ollama": {
        "model": "qwen2.5:14b-instruct",
        "api_key_env": "OLLAMA_UNUSED",
        "config": '        base_url: "http://localhost:11434/v1"',
        "context_size": 32000,
    },
    "groq": {
        "model": "llama-3.1-70b-versatile",
        "api_key_env": "GROQ_API_KEY",
        "config": '        base_url: "https://api.groq.com/openai/v1"',
        "context_size": 128000,
    },
}

def init(
    directory: str = typer.Argument(
        ".",
        help="Directory to create the project in (default: current directory)",
    ),
) -> None:
    """Create a new Digitorn agent application."""
    console.print()
    console.print("[bold blue]digitorn init[/bold blue] - Create a new agent application")
    console.print()

    target = Path(directory).resolve()

    existing = list(target.glob("*.yaml")) + list(target.glob("*.yml"))
    if existing:
        if not Confirm.ask(f"[yellow]Found existing YAML files in {target}. Continue anyway?[/yellow]"):
            raise typer.Exit()

    default_name = target.name if target.name != "." else Path.cwd().name
    name = Prompt.ask("Project name", default=default_name)
    app_id = name.lower().replace(" ", "-").replace("_", "-")

    console.print()
    console.print("[bold]Choose your LLM provider:[/bold]")
    console.print()
    providers = list(_PROVIDERS.keys())
    for i, p in enumerate(providers, 1):
        info = _PROVIDERS[p]
        console.print(f"  [cyan]{i}[/cyan]. {p} ({info['model']})")
    console.print()

    choice = Prompt.ask(
        "Provider",
        choices=[str(i) for i in range(1, len(providers) + 1)],
        default="1",
    )
    provider = providers[int(choice) - 1]
    prov_info = _PROVIDERS[provider]

    include_web = Confirm.ask("Include web search module?", default=False)
    include_db = Confirm.ask("Include database module?", default=False)

    extra_modules = ""
    if include_web:
        extra_modules += "\n  web:\n    config:\n      search:\n        primary: duckduckgo\n"
    if include_db:
        extra_modules += "\n  database: {}\n"

    target.mkdir(parents=True, exist_ok=True)
    skills_dir = target / "skills"
    skills_dir.mkdir(exist_ok=True)
    behavior_dir = target / "behavior"
    behavior_dir.mkdir(exist_ok=True)

    yaml_content = _APP_YAML.format(
        app_id=app_id,
        name=name,
        provider=provider,
        model=prov_info["model"],
        api_key_env=prov_info["api_key_env"],
        provider_config=prov_info["config"],
        context_size=prov_info["context_size"],
        extra_modules=extra_modules,
    )
    (target / "app.yaml").write_text(yaml_content)

    (target / ".digitorn.md").write_text(_DIGITORN_MD.format(name=name))
    (target / ".gitignore").write_text(_GITIGNORE)
    (skills_dir / "commit.md").write_text(_COMMIT_SKILL)
    (skills_dir / "review.md").write_text(_REVIEW_SKILL)
    (behavior_dir / "strict.yaml").write_text(_BEHAVIOR_EXAMPLE)

    console.print()
    console.print(f"[bold green]Project created:[/bold green] {target}")
    console.print()
    console.print("  Files:")
    console.print(f"    [cyan]app.yaml[/cyan]           - Application definition")
    console.print(f"    [cyan].digitorn.md[/cyan]       - Project memory (edit this)")
    console.print(f"    [cyan].gitignore[/cyan]         - Git ignore rules")
    console.print(f"    [cyan]skills/commit.md[/cyan]       - /commit skill")
    console.print(f"    [cyan]skills/review.md[/cyan]       - /review skill")
    console.print(f"    [cyan]behavior/strict.yaml[/cyan]   - Example behavior profile")
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print()
    if provider != "ollama":
        console.print(f"  1. Set your API key:")
        console.print(f"     [dim]export {prov_info['api_key_env']}='your-key-here'[/dim]")
        console.print()
    console.print(f"  2. Run your agent:")
    console.print(f"     [dim]cd {target.name}[/dim]")
    console.print(f"     [dim]digitorn run app.yaml[/dim]")
    console.print()

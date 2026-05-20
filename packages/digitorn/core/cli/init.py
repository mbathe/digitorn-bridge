"""digitorn init - scaffold a new agent application."""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt

console = Console()


_APP_YAML = '''app:
  app_id: {app_id}
  name: "{name}"
  description: "{name} agent built with Digitorn."
  icon: "🤖"
  category: assistant

runtime:
  mode: conversation
  workdir_mode: auto
  direct_modules:
    - filesystem
    - shell
    - memory{extra_direct_modules}
  max_turns: 100
  timeout: 600

agents:
  - id: main
    role: coordinator
    brain:
      provider: {provider}
      backend: {backend}
      model: {model}
      config:
        api_key: {api_key_value}{provider_extra_config}
      temperature: 0.3
      max_tokens: 8192
      context:
        max_tokens: {context_size}
        strategy: summarize
        keep_recent: 10
        auto_compact: true
    system_prompt: |
      You are a helpful AI assistant working in the user's project.
      Read files before editing them. Be concise.

tools:
  modules:
    filesystem: {{}}
    shell: {{}}
    memory:
      config:
        working_memory: true
        todo_list: true{extra_modules}
  capabilities:
    default_policy: auto
    max_risk_level: medium
    grant:
      - module: filesystem
        actions: [read, write, edit, grep, glob]
      - module: shell
        actions: [bash]
      - module: memory
        actions: [remember, task_create, task_update, set_goal]{extra_grants}

ui:
  greeting: |
    Ready. What are we working on?
'''


_DIGITORN_MD = '''# Project Memory

Loaded automatically at the start of every session. Edit this file to
give the agent project-specific context: conventions, architecture,
important paths, team preferences.

## Project
- Name: {name}

## Conventions
- (add coding conventions here)

## Important paths
- Source: src/
- Tests: tests/
'''

_GITIGNORE = '''.env
__pycache__/
*.pyc
.digitorn/
node_modules/
.venv/
venv/
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
   - Quality: clear naming, no duplication, proper error handling
   - Tests: are there tests, do they cover the changes
   - Style: consistent with the rest of the codebase
3. Produce a summary with findings categorized:
   - Critical (must fix before merge)
   - Suggestion (nice to have)
   - Nitpick (style only)
'''


_PROVIDERS: dict[str, dict[str, str | int]] = {
    "deepseek": {
        "model": "deepseek-chat",
        "backend": "openai_compat",
        "api_key_env": "DEEPSEEK_API_KEY",
        "extra_config": '\n        base_url: "https://api.deepseek.com/v1"',
        "context_size": 64000,
    },
    "anthropic": {
        "model": "claude-sonnet-4-5",
        "backend": "anthropic",
        "api_key_env": "ANTHROPIC_API_KEY",
        "extra_config": "",
        "context_size": 200000,
    },
    "openai": {
        "model": "gpt-4o",
        "backend": "openai_compat",
        "api_key_env": "OPENAI_API_KEY",
        "extra_config": '\n        base_url: "https://api.openai.com/v1"',
        "context_size": 128000,
    },
    "openrouter": {
        "model": "anthropic/claude-sonnet-4.5",
        "backend": "openai_compat",
        "api_key_env": "OPENROUTER_API_KEY",
        "extra_config": '\n        base_url: "https://openrouter.ai/api/v1"',
        "context_size": 200000,
    },
    "ollama": {
        "model": "qwen2.5:14b-instruct",
        "backend": "openai_compat",
        "api_key_env": "OLLAMA_UNUSED",
        "extra_config": '\n        base_url: "http://localhost:11434/v1"',
        "context_size": 32000,
    },
    "groq": {
        "model": "llama-3.3-70b-versatile",
        "backend": "openai_compat",
        "api_key_env": "GROQ_API_KEY",
        "extra_config": '\n        base_url: "https://api.groq.com/openai/v1"',
        "context_size": 128000,
    },
}


def _slugify(name: str) -> str:
    return name.lower().replace(" ", "-").replace("_", "-").strip("-") or "my-app"


def init(
    directory: str = typer.Argument(
        ".",
        help="Directory to create the project in (default: current directory).",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        "-n",
        help="Project name. Defaults to the directory name.",
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        "-p",
        help=f"LLM provider ({', '.join(_PROVIDERS)}). Defaults to anthropic.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip all prompts and use defaults.",
    ),
    web: bool = typer.Option(
        False,
        "--web",
        help="Include the web search module.",
    ),
    db: bool = typer.Option(
        False,
        "--db",
        help="Include the database module.",
    ),
) -> None:
    """Create a new Digitorn agent application."""
    console.print()
    console.print("[bold blue]digitorn init[/bold blue] - Create a new agent application")
    console.print()

    target = Path(directory).resolve()
    interactive = sys.stdin.isatty() and not yes

    if target.exists():
        existing = list(target.glob("*.yaml")) + list(target.glob("*.yml"))
        if existing and interactive:
            if not Confirm.ask(
                f"[yellow]Found existing YAML files in {target}. Continue anyway?[/yellow]",
            ):
                raise typer.Exit()
        elif existing and not yes:
            console.print(
                f"[yellow]Found existing YAML files in {target}. "
                f"Pass --yes to overwrite.[/yellow]",
            )
            raise typer.Exit(1)

    default_name = target.name if target.name not in (".", "") else Path.cwd().name
    if name:
        project_name = name
    elif interactive:
        project_name = Prompt.ask("Project name", default=default_name)
    else:
        project_name = default_name
    app_id = _slugify(project_name)

    if provider:
        if provider not in _PROVIDERS:
            console.print(
                f"[bold red]Unknown provider '{provider}'. "
                f"Choose from: {', '.join(_PROVIDERS)}[/bold red]",
            )
            raise typer.Exit(1)
        chosen = provider
    elif interactive:
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
        chosen = providers[int(choice) - 1]
    else:
        chosen = "deepseek"
    prov_info = _PROVIDERS[chosen]

    if interactive and not web and not yes:
        web = Confirm.ask("Include web search module?", default=False)
    if interactive and not db and not yes:
        db = Confirm.ask("Include database module?", default=False)

    extra_direct = ""
    extra_modules = ""
    extra_grants = ""
    if web:
        extra_direct += "\n    - web"
        extra_modules += "\n    web:\n      config:\n        search_backend: duckduckgo"
        extra_grants += (
            "\n      - module: web"
            "\n        actions: [search, fetch]"
        )
    if db:
        extra_direct += "\n    - database"
        extra_modules += "\n    database: {}"
        extra_grants += (
            "\n      - module: database"
            "\n        actions: [sql, schema, list_connections]"
        )

    target.mkdir(parents=True, exist_ok=True)
    skills_dir = target / "skills"
    skills_dir.mkdir(exist_ok=True)

    if chosen == "ollama":
        api_key_value = '"ollama"'
    else:
        api_key_value = '"{{env.' + str(prov_info["api_key_env"]) + '}}"'

    yaml_content = _APP_YAML.format(
        app_id=app_id,
        name=project_name,
        provider=chosen,
        backend=prov_info["backend"],
        model=prov_info["model"],
        api_key_value=api_key_value,
        provider_extra_config=prov_info["extra_config"],
        context_size=prov_info["context_size"],
        extra_direct_modules=extra_direct,
        extra_modules=extra_modules,
        extra_grants=extra_grants,
    )
    (target / "app.yaml").write_text(yaml_content, encoding="utf-8")
    (target / ".digitorn.md").write_text(
        _DIGITORN_MD.format(name=project_name), encoding="utf-8",
    )
    (target / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    (skills_dir / "commit.md").write_text(_COMMIT_SKILL, encoding="utf-8")
    (skills_dir / "review.md").write_text(_REVIEW_SKILL, encoding="utf-8")

    console.print()
    console.print(f"[bold green]Project created:[/bold green] {target}")
    console.print()
    console.print("  Files:")
    console.print("    [cyan]app.yaml[/cyan]           - Application definition")
    console.print("    [cyan].digitorn.md[/cyan]       - Project memory (edit this)")
    console.print("    [cyan].gitignore[/cyan]         - Git ignore rules")
    console.print("    [cyan]skills/commit.md[/cyan]   - /commit skill")
    console.print("    [cyan]skills/review.md[/cyan]   - /review skill")
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print()
    step = 1
    console.print(f"  {step}. Make sure the daemon is running:")
    console.print("     [dim]digitorn status[/dim]")
    console.print("     [dim](if not: digitorn service start)[/dim]")
    console.print()
    step += 1
    console.print(f"  {step}. Sign in to your Digitorn account:")
    console.print("     [dim]digitorn auth login[/dim]")
    console.print()
    step += 1
    if chosen != "ollama":
        console.print(f"  {step}. Set your API key:")
        console.print(
            f"     [dim]export {prov_info['api_key_env']}='your-key-here'[/dim]",
        )
        console.print()
        step += 1
    console.print(f"  {step}. Deploy your app and chat with it:")
    cd_hint = "" if directory in (".", "") else f"     [dim]cd {target.name}[/dim]\n"
    console.print(
        f"{cd_hint}     [dim]digitorn dev deploy app.yaml --scope user[/dim]"
        f"\n     [dim]digitorn dev chat {app_id}[/dim]",
    )
    console.print()

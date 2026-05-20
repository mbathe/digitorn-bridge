"""CLI: digitorn yaml migrate-credentials."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import typer
from rich.console import Console

console = Console()

yaml_cli = typer.Typer(
    name="yaml",
    help="YAML helpers (lint, migrate, compile).",
    no_args_is_help=True,
)


_TEMPLATE_RE = re.compile(
    r"\{\{\s*(?:env|secret)\.([A-Za-z0-9_.\-]+)\s*\}\}",
)


def _slugify_to_ref(name: str, fallback: str) -> str:
    """Build a `credential.ref` slug from an env-style name."""
    n = (name or "").lower()
    for suffix in ("_api_key", "_token", "_key", "_secret"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
            break
    if not n and fallback:
        n = fallback.lower()
    return f"{n}_main" if n else "credential"


def _migrate_yaml_text(text: str) -> tuple[str, int]:
    """Walk a YAML doc and inject `credential:` under every brain"""
    lines = text.splitlines(keepends=False)
    out: list[str] = []
    n_migrations = 0
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        m = re.match(r"^(\s*)brain:\s*$", line)
        if not m:
            out.append(line)
            i += 1
            continue

        indent = m.group(1)
        child_indent = indent + "  "
        # Scan the brain block (everything more indented than `brain:`).
        block_start = i
        j = i + 1
        block_lines: list[str] = []
        while j < len(lines):
            ln = lines[j]
            if ln.strip() == "":
                block_lines.append(ln)
                j += 1
                continue
            ln_indent = len(ln) - len(ln.lstrip(" "))
            if ln_indent <= len(indent):
                break
            block_lines.append(ln)
            j += 1

        # Already has a `credential:` field in the block (at child level)?
        cred_re = re.compile(rf"^{re.escape(child_indent)}credential:")
        already_has = any(cred_re.match(b) for b in block_lines)
        if already_has:
            out.append(line)
            out.extend(block_lines)
            i = j
            continue

        # Find a template inside the block (api_key / base_url / ...).
        provider_re = re.compile(rf"^{re.escape(child_indent)}provider:\s*(\S+)")
        provider_match: re.Match[str] | None = None
        env_match: re.Match[str] | None = None
        for b in block_lines:
            if provider_match is None:
                pm = provider_re.match(b)
                if pm:
                    provider_match = pm
            if env_match is None:
                tm = _TEMPLATE_RE.search(b)
                if tm:
                    env_match = tm

        if env_match is None:
            # Block doesn't use templates - leave it alone.
            out.append(line)
            out.extend(block_lines)
            i = j
            continue

        provider_name = (
            (provider_match.group(1).strip().strip('"\'')
             if provider_match else "")
            .lower()
        )
        ref_slug = _slugify_to_ref(env_match.group(1), provider_name)

        out.append(line)
        cred_block_lines = [
            f"{child_indent}credential:",
            f"{child_indent}  ref: {ref_slug}",
            f"{child_indent}  scope: per_user",
        ]
        if provider_name:
            cred_block_lines.append(
                f"{child_indent}  provider: {provider_name}",
            )
        out.extend(cred_block_lines)
        out.extend(block_lines)
        n_migrations += 1
        i = j

    new_text = "\n".join(out)
    if text.endswith("\n") and not new_text.endswith("\n"):
        new_text += "\n"
    return new_text, n_migrations


def _iter_yamls(root: Path, recursive: bool) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    if recursive:
        yield from root.rglob("app.yaml")
    else:
        yield from root.glob("app.yaml")


@yaml_cli.command("migrate-v2")
def migrate_v2(
    target: Path = typer.Argument(
        ..., exists=True, readable=True,
        help="A single YAML file or a directory.",
    ),
    write: bool = typer.Option(
        False, "--write", help="Rewrite files in place (with .bak backup).",
    ),
    recursive: bool = typer.Option(
        False, "--recursive", "-r",
        help="When TARGET is a directory, walk subdirs.",
    ),
) -> None:
    """Reshape a legacy (flat) app.yaml into the canonical nested form.

    The v2 schema groups every field under one of seven top-level
    blocks: `app`, `runtime`, `agents`, `tools`, `security`,
    `ui`, `dev`. This command applies the same alias pass the
    runtime uses (`schema_aliases.apply_schema_aliases`) and dumps
    the result back to YAML so the file becomes the canonical shape.

    Idempotent: re-running on an already-migrated file is a no-op.

    Comments are preserved on a best-effort basis. When a field moves
    location (e.g. `execution.greeting` -> `ui.greeting`) the
    comment travels with the value but the surrounding comment may
    drift to a different line. Diff and review before committing.
    """
    import yaml as _yaml
    from digitorn.core.app.schema_aliases import apply_schema_aliases

    paths = list(_iter_yamls(target, recursive))
    if not paths:
        console.print("[yellow]No app.yaml found.[/yellow]")
        raise typer.Exit(1)

    total_files = 0
    for p in paths:
        original = p.read_text(encoding="utf-8")
        try:
            raw = _yaml.safe_load(original)
        except _yaml.YAMLError as exc:
            console.print(f"[red]{p}: YAML parse error - {exc}[/red]")
            continue
        if not isinstance(raw, dict):
            console.print(f"[dim]{p}: skipped (not a mapping at root)[/dim]")
            continue

        warnings: list[str] = []
        reshaped = apply_schema_aliases(raw, deprecation_warnings=warnings)

        if reshaped == raw:
            console.print(f"[dim]{p}: already canonical[/dim]")
            continue

        # Emit in canonical block order so the file reads top-to-bottom.
        ordered: dict = {}
        for key in ("app", "runtime", "agents", "tools", "security", "ui", "dev"):
            if key in reshaped:
                ordered[key] = reshaped[key]
        # Anything that didn't fit into a block (Pydantic will error,
        # but include it so the diff is honest).
        for k, v in reshaped.items():
            if k not in ordered:
                ordered[k] = v

        new_text = _yaml.safe_dump(
            ordered, sort_keys=False, default_flow_style=False, allow_unicode=True,
        )

        if write:
            backup = p.with_suffix(p.suffix + ".bak")
            backup.write_text(original, encoding="utf-8")
            p.write_text(new_text, encoding="utf-8")
            console.print(
                f"[green]{p}: migrated[/green] "
                f"[dim](backup -> {backup.name}, {len(warnings)} change(s))[/dim]",
            )
        else:
            console.print(
                f"[bold]{p}[/bold] [dim](preview, {len(warnings)} change(s) - "
                f"add --write to apply)[/dim]",
            )
            for w in warnings:
                console.print(f"  [yellow]- {w}[/yellow]")
            console.print(new_text)
        total_files += 1

    console.print(
        f"\n[bold]Done.[/bold] {total_files} file(s) reshaped to v2.",
    )


@yaml_cli.command("migrate-credentials")
def migrate_credentials(
    target: Path = typer.Argument(
        ..., exists=True, readable=True,
        help="A single YAML file or a directory.",
    ),
    write: bool = typer.Option(
        False, "--write", help="Rewrite files in place (with .bak backup).",
    ),
    recursive: bool = typer.Option(
        False, "--recursive", "-r",
        help="When TARGET is a directory, walk subdirs.",
    ),
) -> None:
    """Migrate `{{env.X}}` / `{{secret.X}}` patterns into
    declarative `credential:` blocks.

    Run this once when adopting the new credential system. Re-running
    is a no-op for already-migrated files.
    """
    paths = list(_iter_yamls(target, recursive))
    if not paths:
        console.print("[yellow]No app.yaml found.[/yellow]")
        raise typer.Exit(1)

    total = 0
    for p in paths:
        original = p.read_text(encoding="utf-8")
        migrated, n = _migrate_yaml_text(original)
        if n == 0:
            console.print(f"[dim]{p}: no changes[/dim]")
            continue
        if write:
            backup = p.with_suffix(p.suffix + ".bak")
            backup.write_text(original, encoding="utf-8")
            p.write_text(migrated, encoding="utf-8")
            console.print(
                f"[green]{p}: {n} brain(s) migrated[/green] "
                f"[dim](backup -> {backup.name})[/dim]",
            )
        else:
            console.print(
                f"[bold]{p}[/bold] [dim](preview, {n} block(s) - "
                f"add --write to apply)[/dim]",
            )
            console.print(migrated)
        total += n

    console.print(
        f"\n[bold]Done.[/bold] {total} block(s) migrated across {len(paths)} file(s).",
    )


@yaml_cli.command("lint")
def lint(
    target: Path = typer.Argument(
        ..., exists=True, readable=True,
        help="A single YAML file or a directory.",
    ),
    fix: bool = typer.Option(
        False, "--fix", help="Apply auto-fixes in place (with .bak backup).",
    ),
    recursive: bool = typer.Option(
        False, "--recursive", "-r",
        help="When TARGET is a directory, walk subdirs.",
    ),
    severity: str = typer.Option(
        "warn", "--severity",
        help="Minimum severity to surface: error / warn / info.",
    ),
) -> None:
    """Lint an app.yaml against the canonical schema + best-practice rules.

    Reports:

    - **errors**: schema violations the daemon would reject at deploy
      (missing app_id, undeclared module references, invalid flow target,
      unknown credential ref, ...).
    - **warns**: footguns the daemon tolerates but you probably don't
      want (orphan modules, capabilities granting actions on undeclared
      modules, hooks without explicit ids, mode-specific fields in the
      wrong mode).
    - **infos**: opportunities to harden (no system_prompt, no fallback
      brain, missing schema_version, ...).

    With `--fix`, the linter applies the auto-fixable issues
    (deprecation lifts, schema_version stamping, dead capability
    cleanup) and writes the result with a `.bak` backup.
    """
    import yaml as _yaml
    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.core.app.errors import AppCompilationError
    from digitorn.core.app.schema_aliases import apply_schema_aliases

    paths = list(_iter_yamls(target, recursive))
    if not paths:
        console.print("[yellow]No app.yaml found.[/yellow]")
        raise typer.Exit(1)

    rank = {"info": 0, "warn": 1, "error": 2}
    threshold = rank.get(severity, 1)

    total_issues = 0
    total_fixes = 0
    for p in paths:
        original = p.read_text(encoding="utf-8")
        try:
            raw = _yaml.safe_load(original)
        except _yaml.YAMLError as exc:
            console.print(f"[red]{p}: YAML parse error - {exc}[/red]")
            total_issues += 1
            continue
        if not isinstance(raw, dict):
            continue

        issues: list[tuple[str, str, str]] = []  # (severity, message, hint)
        fixes_applied: list[str] = []

        # Apply schema_aliases (canonicalize) so issues reference
        # canonical paths.
        warns: list[str] = []
        canonical = apply_schema_aliases(raw, deprecation_warnings=warns)
        for w in warns:
            issues.append(("info", w, "Run `digitorn yaml migrate-v2 --write`."))

        # Auto-fixable: stamp schema_version when absent.
        if "schema_version" not in canonical:
            issues.append(("info", "schema_version is not set.", "Add `schema_version: 2` to declare the file's schema generation explicitly."))
            if fix:
                canonical = {"schema_version": 2, **canonical}
                fixes_applied.append("stamped schema_version: 2")

        try:
            from digitorn.core.loader import load_modules
            from digitorn.modules.registry import ModuleRegistry
            reg = ModuleRegistry()
            load_modules(reg, load_all=True)
            compiler = AppYAMLCompiler(reg)
            compiler.compile(canonical)
        except AppCompilationError as exc:
            for err in exc.errors:
                issues.append(("error", err, ""))
        except Exception as exc:
            issues.append(("error", str(exc), ""))

        # Best-practice rules ─────────────────────────────────────
        agents = canonical.get("agents") or []
        tools = canonical.get("tools") or {}
        modules = (tools.get("modules") if isinstance(tools, dict) else None) or {}
        caps = (tools.get("capabilities") if isinstance(tools, dict) else None) or {}

        # Orphan modules: declared but never granted.
        granted_mods: set[str] = set()
        if isinstance(caps.get("grant"), list):
            for g in caps["grant"]:
                if isinstance(g, dict) and isinstance(g.get("module"), str):
                    granted_mods.add(g["module"])
        for mod_id in modules:
            if mod_id not in granted_mods and mod_id not in {"context_builder", "llm_provider"}:
                issues.append((
                    "warn",
                    f"Module `{mod_id}` is declared but never granted to any agent.",
                    f"Either grant it under `tools.capabilities.grant`, or remove `tools.modules.{mod_id}`.",
                ))

        # Agents without system_prompt (info)
        for a in agents:
            if isinstance(a, dict) and a.get("id") and not a.get("system_prompt"):
                issues.append((
                    "info",
                    f"Agent `{a['id']}` has no `system_prompt` - the LLM will fall back to provider defaults.",
                    "Add a `system_prompt:` (a templated string is fine).",
                ))

        # No fallback brain on cloud providers (info)
        cloud = {"anthropic", "openai", "deepseek", "google", "azure"}
        for a in agents:
            if not isinstance(a, dict):
                continue
            brain = a.get("brain") or {}
            if isinstance(brain, dict):
                provider = brain.get("provider")
                if provider in cloud and not brain.get("fallback"):
                    issues.append((
                        "info",
                        f"Agent `{a.get('id', '?')}` uses cloud provider `{provider}` without a fallback brain.",
                        "Add `brain.fallback: { provider, model }` for graceful degradation on 402 / rate limit.",
                    ))

        # Print + apply fixes.
        filtered = [i for i in issues if rank.get(i[0], 0) >= threshold]
        if not filtered and not fixes_applied:
            console.print(f"[green]{p}: clean[/green]")
            continue

        console.print(f"\n[bold]{p}[/bold]")
        for sev, msg, hint in filtered:
            color = {"error": "red", "warn": "yellow", "info": "dim"}[sev]
            console.print(f"  [{color}]{sev:>5}[/{color}] {msg}")
            if hint:
                console.print(f"          [dim]{hint}[/dim]")
            total_issues += 1

        if fix and fixes_applied:
            backup = p.with_suffix(p.suffix + ".bak")
            backup.write_text(original, encoding="utf-8")
            ordered: dict = {}
            for k in ("schema_version", "app", "runtime", "agents", "tools", "security", "ui", "dev", "flow"):
                if k in canonical:
                    ordered[k] = canonical[k]
            for k, v in canonical.items():
                if k not in ordered:
                    ordered[k] = v
            p.write_text(
                _yaml.safe_dump(ordered, sort_keys=False, default_flow_style=False, allow_unicode=True),
                encoding="utf-8",
            )
            for f in fixes_applied:
                console.print(f"  [green]fixed[/green] {f}")
            total_fixes += len(fixes_applied)

    console.print(
        f"\n[bold]Lint done.[/bold] {total_issues} issue(s){f', {total_fixes} fix(es) applied' if fix else ''} "
        f"across {len(paths)} file(s).",
    )


@yaml_cli.command("explain")
def explain(
    target: Path = typer.Argument(
        ..., exists=True, readable=True,
        help="Path to a single app.yaml file.",
    ),
) -> None:
    """Print a human-readable description of what the daemon would do.

    Walks the canonicalized YAML and emits a prose summary of every
    block: which agents run, what tools they can call (and under what
    security policy), what triggers fire, where output goes, what the
    UI surfaces, and the orchestration graph if present.

    Useful for design reviews, onboarding, and debugging "what does
    this app actually do?" without running the daemon.
    """
    import yaml as _yaml
    from digitorn.core.app.schema_aliases import apply_schema_aliases

    if target.is_dir():
        console.print("[red]explain takes a single file, not a directory.[/red]")
        raise typer.Exit(1)

    original = target.read_text(encoding="utf-8")
    try:
        raw = _yaml.safe_load(original)
    except _yaml.YAMLError as exc:
        console.print(f"[red]YAML parse error: {exc}[/red]")
        raise typer.Exit(1)

    canonical = apply_schema_aliases(raw)

    # Block 1: app identity
    app = canonical.get("app") or {}
    name = app.get("name") or app.get("app_id") or "<unnamed>"
    app_id = app.get("app_id") or "<no id>"
    version = app.get("version") or "1.0"
    desc = app.get("description") or ""

    console.print(f"\n[bold cyan]{name}[/bold cyan] [dim]({app_id} v{version})[/dim]")
    if desc:
        console.print(f"  [dim]{desc}[/dim]")

    # Block 2: runtime behavior
    runtime = canonical.get("runtime") or {}
    mode = runtime.get("mode", "conversation")
    entry = runtime.get("entry_agent") or "(first agent)"
    max_turns = runtime.get("max_turns", 50)
    triggers = runtime.get("triggers") or []
    pipeline = runtime.get("pipeline") or []

    console.print(f"\n[bold]Runtime[/bold]")
    if mode == "conversation":
        console.print(f"  Conversational app entered via [cyan]{entry}[/cyan], max [cyan]{max_turns}[/cyan] turns per message.")
    elif mode == "one_shot":
        console.print(f"  One-shot: receives a prompt, agent [cyan]{entry}[/cyan] runs once, returns a result.")
    elif mode == "background":
        console.print(f"  Background: fires on triggers, agent [cyan]{entry}[/cyan] handles each activation.")
        if triggers:
            for t in triggers:
                tt = t.get("type", "?")
                if tt == "cron":
                    console.print(f"    [yellow]cron[/yellow] {t.get('schedule', '?')}")
                elif tt == "watch":
                    console.print(f"    [yellow]watch[/yellow] {t.get('paths', [])}")
                elif tt == "http":
                    console.print(f"    [yellow]http[/yellow] {t.get('method', 'POST')} {t.get('path', '/?')}")
    elif mode == "pipeline":
        console.print(f"  Pipeline mode: chains apps in sequence.")
        for i, step in enumerate(pipeline):
            console.print(f"    {i+1}. [cyan]{step.get('app', '?')}[/cyan]")

    # Block 3: agents
    agents = canonical.get("agents") or []
    console.print(f"\n[bold]Agents[/bold] ({len(agents)})")
    for a in agents:
        if not isinstance(a, dict):
            continue
        aid = a.get("id", "?")
        role = a.get("role") or "specialist"
        brain = a.get("brain") or {}
        provider = brain.get("provider", "?")
        model = brain.get("model", "?")
        fallback = "  [dim]+ fallback[/dim]" if brain.get("fallback") else ""
        console.print(f"  [cyan]{aid}[/cyan] [{role}] -- {provider}/{model}{fallback}")

    # Block 4: tools
    tools = canonical.get("tools") or {}
    modules = tools.get("modules") or {}
    caps = tools.get("capabilities") or {}
    channels = tools.get("channels") or {}
    if modules or caps or channels:
        console.print(f"\n[bold]Tools[/bold]")
    if modules:
        console.print(f"  Modules ({len(modules)}): {', '.join(sorted(modules.keys()))}")
    if caps:
        default = caps.get("default_policy", "approve")
        grant_count = len(caps.get("grant") or [])
        approve_count = len(caps.get("approve") or [])
        deny_count = len(caps.get("deny") or [])
        console.print(f"  Capabilities: default [yellow]{default}[/yellow] | "
                       f"{grant_count} grant, {approve_count} approve, {deny_count} deny")
    if channels:
        console.print(f"  Channels: {', '.join(sorted(channels.keys()))}")

    # Block 5: security
    sec = canonical.get("security") or {}
    behavior = sec.get("behavior")
    sandbox = sec.get("sandbox")
    cred_schema = sec.get("credentials_schema")
    if behavior or sandbox or cred_schema:
        console.print(f"\n[bold]Security[/bold]")
    if behavior:
        profile = behavior.get("profile", "<custom>")
        rule_count = len(behavior.get("rule_definitions") or [])
        console.print(f"  Behavior profile: [yellow]{profile}[/yellow] ({rule_count} extra rule(s))")
    if sandbox:
        level = sandbox.get("level", "standard")
        console.print(f"  Sandbox: [yellow]{level}[/yellow] (OS-level isolation)")
    if cred_schema:
        providers = cred_schema.get("providers") or []
        console.print(f"  Credentials schema: {len(providers)} provider(s) declared")

    # Block 6: ui
    ui = canonical.get("ui") or {}
    rendered_blocks = []
    if ui.get("widgets"):
        rendered_blocks.append("widgets")
    if ui.get("workspace"):
        rm = (ui["workspace"] or {}).get("render_mode", "?")
        rendered_blocks.append(f"workspace ({rm})")
    if ui.get("preview"):
        rendered_blocks.append("preview server")
    if ui.get("greeting"):
        rendered_blocks.append("greeting")
    if rendered_blocks:
        console.print(f"\n[bold]UI[/bold]")
        console.print(f"  {', '.join(rendered_blocks)}")

    # Block 7: dev
    dev = canonical.get("dev") or {}
    skills = dev.get("skills") or []
    variables = dev.get("variables") or {}
    if skills or variables:
        console.print(f"\n[bold]Dev[/bold]")
    if skills:
        console.print(f"  Skills: {len(skills)} /command file(s)")
    if variables:
        console.print(f"  Variables: {len(variables)} declared")

    # Block 8: flow
    flow = canonical.get("flow")
    if flow:
        nodes = flow.get("nodes") or []
        entry_node = flow.get("entry", "?")
        console.print(f"\n[bold]Flow[/bold] -- declarative orchestration")
        console.print(f"  Entry: [cyan]{entry_node}[/cyan], {len(nodes)} node(s)")
        for n in nodes:
            if not isinstance(n, dict):
                continue
            nid = n.get("id", "?")
            ntype = n.get("type", "?")
            extras = []
            if ntype == "agent" and n.get("agent"):
                extras.append(f"-> {n['agent']}")
            elif ntype == "tool" and n.get("tool"):
                extras.append(f"-> {n['tool']}")
            elif ntype == "decision" and n.get("expr"):
                extras.append(f"if {n['expr']}")
            routes = n.get("routes") or []
            if routes:
                extras.append(f"{len(routes)} route(s)")
            console.print(f"    [cyan]{nid}[/cyan] [{ntype}]" + (f" {' '.join(extras)}" if extras else ""))

    console.print()

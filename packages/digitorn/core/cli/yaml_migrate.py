"""CLI: digitorn yaml migrate-credentials.

Walks an app.yaml file (or every YAML in a directory) and rewrites
legacy credential patterns into the new declarative `credential:`
block.

Patterns recognised:
  * Inline brain config carrying ``api_key: "{{env.X}}"`` or
    ``api_key: "{{secret.X}}"``. Migrated to:
      brain:
        ...
        credential:
          ref: <slug>          # derived from the env/secret name
          scope: per_user      # default - user can edit afterwards
          provider: <name>     # taken from `brain.provider` when present
        config:
          api_key: "{{env.X}}"  # left as fallback for dev
          ...
  * Module config carrying the same templates - same migration path.

Behaviour:
  * Files are NEVER edited in place by default. The migrated YAML
    goes to stdout (so the user can pipe it to a diff tool first).
  * ``--write`` rewrites in place after creating a `<file>.bak`
    backup.
  * Multiple files via ``--recursive``.

The migration is conservative: it ADDS the `credential:` block but
keeps the existing template strings as a fallback. A second run of
the CLI is a no-op (it detects the existing block and skips).
"""

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
    """Build a `credential.ref` slug from an env-style name.

    `DEEPSEEK_API_KEY` -> `deepseek_main`
    `OPENAI_API_KEY`   -> `openai_main`
    `XYZ_TOKEN`        -> `xyz_main`
    Falls back to `<provider>_main` when the slug doesn't carry a
    recognisable provider hint.
    """
    n = (name or "").lower()
    for suffix in ("_api_key", "_token", "_key", "_secret"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
            break
    if not n and fallback:
        n = fallback.lower()
    return f"{n}_main" if n else "credential"


def _migrate_yaml_text(text: str) -> tuple[str, int]:
    """Walk a YAML doc and inject `credential:` under every brain
    block that carries a legacy template AND doesn't already have
    one.

    The implementation is line-oriented (no full YAML parse) so the
    output preserves comments, blank lines, and the user's exact
    indentation. Return value: (new_text, n_migrations).
    """
    lines = text.splitlines(keepends=False)
    out: list[str] = []
    n_migrations = 0
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # Detect a brain block header. Two indentations are common:
        #   "    brain:"  (4-space, agent brain under list item)
        #   "  brain:"    (2-space, behavior brain)
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

        # Insert the credential block after the brain header (i.e.
        # before the first child line). This places it at the top of
        # the brain block so it's clearly visible.
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
    """Migrate ``{{env.X}}`` / ``{{secret.X}}`` patterns into
    declarative ``credential:`` blocks.

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

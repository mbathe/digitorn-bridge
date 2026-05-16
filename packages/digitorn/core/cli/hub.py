"""Hub CLI commands.

Two layers of commands:

**Offline / direct hub** (no daemon needed):
    digitorn hub pack <package_dir> [-o output]
        Bundle a package directory into a .tar.gz archive, with sane
        exclusions (node_modules, __pycache__, .git, ...).
    digitorn hub publish <archive> --publisher <slug> [--hub URL] [--token TOKEN]
        Upload an archive to a remote hub via API token.

**Daemon-mediated** (use the local daemon's session):
    digitorn hub login                              Authenticate against the hub
    digitorn hub logout                             Drop cached hub session
    digitorn hub me                                 Who am I on the hub?
    digitorn hub search <query> [--tag T] [--category C]
    digitorn hub install <publisher>/<package>[@version]
"""
from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import tomllib
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from digitorn.core.cli.auth_helpers import daemon_request

console = Console()

hub_cli = typer.Typer(
    name="hub",
    help="Publish, search and install Digitorn applications from the hub.",
    no_args_is_help=True,
)

_DEFAULT_DAEMON = "http://127.0.0.1:8000"

_EXCLUDE_DIRS = {
    ".git", "__pycache__", "node_modules", ".digitorn", ".pytest_cache",
    ".ruff_cache", ".vscode", ".idea", ".venv", ".venv312", ".turbo",
    ".next", ".cache", ".output", ".svelte-kit", "build", ".mypy_cache",
}
_EXCLUDE_FILE_SUFFIXES = (".pyc", ".pyo")
_EXCLUDE_FILE_NAMES = {".DS_Store", "Thumbs.db", ".env", ".env.local"}


# ────────────────────────────────────────────────────────────────────
# Offline: pack
# ────────────────────────────────────────────────────────────────────


def _read_manifest(package_dir: Path) -> dict[str, Any]:
    toml_path = package_dir / "package.toml"
    if not toml_path.is_file():
        console.print(f"[red]Missing package.toml in[/red] {package_dir}")
        raise typer.Exit(1)
    try:
        return tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        console.print(f"[red]package.toml is invalid:[/red] {exc}")
        raise typer.Exit(1)


def _walk_package(root: Path):
    """Yield (absolute_path, arcname) pairs for everything that should ship."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        rel_dir = Path(dirpath).relative_to(root)
        for fname in filenames:
            if fname in _EXCLUDE_FILE_NAMES:
                continue
            if fname.endswith(_EXCLUDE_FILE_SUFFIXES):
                continue
            full = Path(dirpath) / fname
            arc = (rel_dir / fname).as_posix()
            yield full, arc


@hub_cli.command(name="pack")
def pack_package(
    package_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Bundle <package_dir> into a .tar.gz hub archive.

    Excludes: node_modules, __pycache__, .git, .venv*, build/, dist/,
    .vscode, .idea, .DS_Store, Thumbs.db, *.pyc, .env, .env.local.
    """
    package_dir = package_dir.resolve()
    manifest = _read_manifest(package_dir)
    pkg = manifest.get("package", {})
    pkg_id = pkg.get("id", "")
    version = pkg.get("version", "")
    if not pkg_id or not version:
        console.print("[red]package.toml: 'id' and 'version' are required[/red]")
        raise typer.Exit(1)

    if output is None:
        output = Path.cwd() / f"{pkg_id}-{version}.tar.gz"

    output.parent.mkdir(parents=True, exist_ok=True)
    file_count = 0
    with tarfile.open(output, "w:gz") as tar:
        for full, arc in _walk_package(package_dir):
            tar.add(full, arcname=arc)
            file_count += 1

    if file_count == 0:
        output.unlink(missing_ok=True)
        console.print("[red]No files to pack (everything excluded?)[/red]")
        raise typer.Exit(1)

    size_mb = output.stat().st_size / (1024 * 1024)
    console.print(
        Panel.fit(
            f"[green]✓ Packed[/green] [cyan]{pkg_id}[/cyan] v{version}\n"
            f"  files : {file_count}\n"
            f"  size  : {size_mb:.2f} MB\n"
            f"  output: {output}",
            title="hub pack",
        )
    )


# ────────────────────────────────────────────────────────────────────
# Direct hub: publish
# ────────────────────────────────────────────────────────────────────


def _hub_url(explicit: str | None) -> str:
    url = (explicit or os.environ.get("DIGITORN_HUB_URL", "")).strip()
    if not url:
        console.print(
            "[red]Hub URL not set.[/red] Pass --hub or set DIGITORN_HUB_URL."
        )
        raise typer.Exit(1)
    return url.rstrip("/")


def _read_archive_manifest(archive: Path) -> dict[str, Any]:
    if not archive.is_file():
        console.print(f"[red]Archive not found:[/red] {archive}")
        raise typer.Exit(1)
    try:
        with tarfile.open(archive, "r:gz") as tf:
            for member in tf.getmembers():
                name = member.name.replace("\\", "/")
                # Tolerate optional top-level dir
                if name.endswith("/package.toml") or name == "package.toml":
                    f = tf.extractfile(member)
                    if f is None:
                        continue
                    return tomllib.loads(f.read().decode("utf-8"))
    except tarfile.TarError as exc:
        console.print(f"[red]Bad archive: {exc}[/red]")
        raise typer.Exit(1)
    console.print("[red]No package.toml found inside the archive[/red]")
    raise typer.Exit(1)


@hub_cli.command(name="publish")
def publish_archive(
    archive: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    publisher: Annotated[str, typer.Option("--publisher", "-p", help="Publisher slug")],
    hub: Annotated[str | None, typer.Option("--hub", help="Hub URL (or env DIGITORN_HUB_URL)")] = None,
    token: Annotated[
        str | None,
        typer.Option(
            "--token", "-t",
            help="Hub API token (or env DIGITORN_HUB_TOKEN)",
        ),
    ] = None,
) -> None:
    """Upload a packed .tar.gz archive to the hub."""
    hub_url = _hub_url(hub)
    api_token = (token or os.environ.get("DIGITORN_HUB_TOKEN", "")).strip()
    if not api_token:
        console.print(
            "[red]Hub API token not set.[/red] Pass --token or set DIGITORN_HUB_TOKEN."
        )
        raise typer.Exit(1)

    manifest = _read_archive_manifest(archive)
    pkg = manifest.get("package", {})
    pkg_id = pkg.get("id", "")
    version = pkg.get("version", "")
    if not pkg_id or not version:
        console.print("[red]Archive package.toml missing id or version[/red]")
        raise typer.Exit(1)

    upload_url = (
        f"{hub_url}/api/v1/publishers/{publisher}/packages/{pkg_id}/versions"
    )
    console.print(
        f"Uploading [cyan]{pkg_id}[/cyan] v[yellow]{version}[/yellow] to "
        f"[blue]{publisher}[/blue] @ {hub_url} …"
    )
    with archive.open("rb") as fh:
        files = {
            "file": (archive.name, fh, "application/gzip"),
        }
        data = {"version": version}
        try:
            r = httpx.post(
                upload_url,
                headers={"Authorization": f"Bearer {api_token}"},
                files=files,
                data=data,
                timeout=300.0,
            )
        except httpx.RequestError as exc:
            console.print(f"[red]Upload failed:[/red] {exc}")
            raise typer.Exit(1)

    if r.status_code >= 400:
        console.print(f"[red]Hub returned {r.status_code}:[/red] {r.text[:500]}")
        raise typer.Exit(1)
    body = r.json()
    console.print(
        Panel.fit(
            f"[green]✓ Published[/green] [cyan]{publisher}/{pkg_id}[/cyan] "
            f"v[yellow]{version}[/yellow]\n"
            f"  archive_size  : {body['version']['archive_size']} bytes\n"
            f"  archive_sha256: {body['version']['archive_sha256']}\n"
            f"  released_at   : {body['version']['released_at']}",
            title="hub publish",
        )
    )


# ────────────────────────────────────────────────────────────────────
# Daemon-mediated: install
# ────────────────────────────────────────────────────────────────────
#
# Browse / search / detail / reviews / reports / stats CLI commands
# were removed when the Hub started accepting central RS256 JWTs
# natively (see digitorn_hub.auth.central). Use the web client at
# https://app.digitorn.ai or hit https://hub.digitorn.ai/api/v1/*
# directly with a Bearer token from the central auth service.
#
# Install stays here because it has to download archives + atomically
# deploy them on the daemon's filesystem - intrinsically a
# daemon-local operation.

@hub_cli.command(name="install")
def hub_install(
    target: Annotated[
        str,
        typer.Argument(help="<publisher>/<package_id>[@<version>]"),
    ],
    scope: Annotated[str, typer.Option("--scope", help="user|system")] = "user",
    accept: Annotated[bool, typer.Option("--accept", help="Skip permissions prompt")] = False,
    daemon: Annotated[str, typer.Option("--daemon", "-d")] = _DEFAULT_DAEMON,
) -> None:
    """Install a hub package into the local daemon."""
    if "/" not in target:
        console.print("[red]Target must be <publisher>/<package_id>[@version][/red]")
        raise typer.Exit(1)
    publisher_pkg, _, version = target.partition("@")
    publisher, _, package_id = publisher_pkg.partition("/")
    if not publisher or not package_id:
        console.print("[red]Invalid target - expected <publisher>/<package_id>[/red]")
        raise typer.Exit(1)

    body: dict[str, Any] = {
        "publisher": publisher,
        "package_id": package_id,
        "scope": scope,
        "accept_permissions": accept,
    }
    if version:
        body["version"] = version

    r = daemon_request("post", f"{daemon}/api/hub/install", json=body)
    if r.status_code == 409:
        try:
            detail = r.json().get("detail", {})
        except Exception:
            detail = {}
        if detail.get("error") == "permissions_required":
            perms = detail.get("permissions", {})
            console.print(
                Panel.fit(
                    json.dumps(perms, indent=2),
                    title=f"Permissions required for {detail.get('package_id')}",
                    border_style="yellow",
                )
            )
            if not typer.confirm("Accept these permissions and proceed?"):
                console.print("[yellow]Aborted[/yellow]")
                raise typer.Exit(0)
            body["accept_permissions"] = True
            r = daemon_request("post", f"{daemon}/api/hub/install", json=body)
    if r.status_code >= 400:
        console.print(f"[red]Install failed ({r.status_code}):[/red] {r.text[:500]}")
        raise typer.Exit(1)
    out = r.json()
    # The daemon wraps the InstallResult under ``out['result']`` so the
    # top-level shape can stay stable for future extensions
    # (``out`` itself only carries the request echo: package_id,
    # publisher, scope). Read display fields from the nested result.
    result = out.get("result", {})
    console.print(
        f"[green]✓ Installed[/green] [cyan]{out['package_id']}[/cyan]"
        f" v[yellow]{result.get('version', '?')}[/yellow]"
        f" (scope={out['scope']}, deployed={result.get('deployed', False)})"
    )

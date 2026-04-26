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
# Daemon-mediated: login / logout / me / search / install
# ────────────────────────────────────────────────────────────────────


@hub_cli.command(name="login")
def hub_login(
    daemon: Annotated[str, typer.Option("--daemon", "-d")] = _DEFAULT_DAEMON,
    email: Annotated[str | None, typer.Option("--email", "-e")] = None,
) -> None:
    """Log into the hub via the local daemon. Caches the JWT per daemon user."""
    if not email:
        email = typer.prompt("Hub email")
    password = typer.prompt("Hub password", hide_input=True)
    r = daemon_request(
        "post",
        f"{daemon}/api/hub/login",
        json={"email": email, "password": password},
    )
    if r.status_code >= 400:
        console.print(f"[red]Login failed ({r.status_code}):[/red] {r.text[:300]}")
        raise typer.Exit(1)
    body = r.json()
    console.print(
        f"[green]✓ Logged in[/green] as [cyan]{body['hub_user']['email']}[/cyan]"
        f" on {body['hub_url']}"
    )


@hub_cli.command(name="logout")
def hub_logout(
    daemon: Annotated[str, typer.Option("--daemon", "-d")] = _DEFAULT_DAEMON,
) -> None:
    r = daemon_request("post", f"{daemon}/api/hub/logout")
    if r.status_code not in (200, 204):
        console.print(f"[red]Logout failed:[/red] {r.text[:200]}")
        raise typer.Exit(1)
    console.print("[green]✓ Hub session cleared[/green]")


@hub_cli.command(name="me")
def hub_me(
    daemon: Annotated[str, typer.Option("--daemon", "-d")] = _DEFAULT_DAEMON,
) -> None:
    r = daemon_request("get", f"{daemon}/api/hub/me")
    if r.status_code >= 400:
        console.print(f"[red]{r.status_code}:[/red] {r.text[:300]}")
        raise typer.Exit(1)
    body = r.json()
    if not body.get("logged_in"):
        console.print(f"[yellow]Not logged in[/yellow] (hub: {body.get('hub_url')})")
        return
    console.print(
        f"[green]Logged in[/green] as [cyan]{body['hub_user']['email']}[/cyan]"
        f" on {body['hub_url']}"
    )


@hub_cli.command(name="search")
def hub_search(
    query: Annotated[str, typer.Argument(help="Free-text search (semantic + FTS)")] = "",
    tag: Annotated[list[str] | None, typer.Option("--tag", "-t")] = None,
    category: Annotated[str | None, typer.Option("--category", "-c")] = None,
    risk: Annotated[str | None, typer.Option("--risk")] = None,
    publisher: Annotated[str | None, typer.Option("--publisher", "-p")] = None,
    include_unverified: Annotated[
        bool,
        typer.Option(
            "--all", "-a",
            help="Include community (non-verified) publishers in results",
        ),
    ] = False,
    page: Annotated[int, typer.Option("--page")] = 1,
    page_size: Annotated[int, typer.Option("--limit")] = 20,
    json_out: Annotated[bool, typer.Option("--json", help="Output JSON")] = False,
    daemon: Annotated[str, typer.Option("--daemon", "-d")] = _DEFAULT_DAEMON,
) -> None:
    """Search the hub. Hybrid semantic + FTS — finds packages by intent.

    By default, only VERIFIED publishers are shown. Use --all to include community.
    """
    params: dict[str, Any] = {
        "q": query, "page": page, "page_size": page_size,
        "include_unverified": str(include_unverified).lower(),
    }
    if category: params["category"] = category
    if risk: params["risk_level"] = risk
    if publisher: params["publisher"] = publisher
    if tag:
        params["tag"] = tag
    r = daemon_request("get", f"{daemon}/api/hub/search", params=params)
    if r.status_code >= 400:
        console.print(f"[red]{r.status_code}:[/red] {r.text[:300]}")
        raise typer.Exit(1)
    body = r.json()

    if json_out:
        sys.stdout.write(json.dumps(body, indent=2, ensure_ascii=False) + "\n")
        return

    hits = body.get("hits", [])
    if not hits:
        console.print(f"[yellow]No results for[/yellow] {query!r}")
        return
    suffix = "" if include_unverified else " (verified publishers only — pass --all to include community)"
    table = Table(title=f"Hub search — {body.get('total', 0)} hit(s) for {query!r}{suffix}")
    table.add_column("Score", justify="right", style="dim")
    table.add_column("Package")
    table.add_column("Latest", style="yellow")
    table.add_column("Tags", style="cyan")
    table.add_column("Description")
    for h in hits:
        badge = "[green]V[/green]" if h.get("publisher_verified") else "[dim]C[/dim]"
        table.add_row(
            f"{h.get('score', 0):.4f}",
            f"{badge} [blue]{h['publisher_slug']}[/blue]/[cyan]{h['package_id']}[/cyan]",
            h.get("latest_version") or "-",
            ", ".join(h.get("tags", [])[:3]),
            (h.get("description") or "")[:60],
        )
    console.print(table)


def _split_target(target: str) -> tuple[str, str]:
    if "/" not in target:
        console.print("[red]Target must be <publisher>/<package_id>[/red]")
        raise typer.Exit(1)
    publisher, _, package_id = target.partition("/")
    if not publisher or not package_id:
        console.print("[red]Invalid target — expected <publisher>/<package_id>[/red]")
        raise typer.Exit(1)
    return publisher, package_id


@hub_cli.command(name="review")
def hub_review(
    target: Annotated[str, typer.Argument(help="<publisher>/<package_id>")],
    rating: Annotated[int, typer.Option("--rating", "-r", min=1, max=5)] = ...,
    body: Annotated[str | None, typer.Option("--body", "-b")] = None,
    daemon: Annotated[str, typer.Option("--daemon", "-d")] = _DEFAULT_DAEMON,
) -> None:
    """Submit a star rating (and optional text) for a package.

    Re-running for the same package updates your existing review.
    """
    publisher, package_id = _split_target(target)
    payload: dict[str, Any] = {"rating": rating}
    if body:
        payload["body"] = body
    r = daemon_request(
        "post",
        f"{daemon}/api/hub/packages/{publisher}/{package_id}/reviews",
        json=payload,
    )
    if r.status_code >= 400:
        console.print(f"[red]{r.status_code}:[/red] {r.text[:300]}")
        raise typer.Exit(1)
    out = r.json()
    console.print(
        f"[green]+[/green] Review {'updated' if out.get('updated_at') != out.get('created_at') else 'submitted'}: "
        f"[yellow]{'*' * out['rating']}[/yellow]"
    )


@hub_cli.command(name="reviews")
def hub_reviews(
    target: Annotated[str, typer.Argument(help="<publisher>/<package_id>")],
    sort: Annotated[
        str,
        typer.Option(
            "--sort", "-s",
            help="recent | rating_desc | rating_asc",
        ),
    ] = "recent",
    page: Annotated[int, typer.Option("--page")] = 1,
    page_size: Annotated[int, typer.Option("--limit")] = 10,
    json_out: Annotated[bool, typer.Option("--json")] = False,
    daemon: Annotated[str, typer.Option("--daemon", "-d")] = _DEFAULT_DAEMON,
) -> None:
    """List reviews for a package (with rating distribution + average)."""
    publisher, package_id = _split_target(target)
    r = daemon_request(
        "get",
        f"{daemon}/api/hub/packages/{publisher}/{package_id}/reviews",
        params={"sort": sort, "page": page, "page_size": page_size},
    )
    if r.status_code >= 400:
        console.print(f"[red]{r.status_code}:[/red] {r.text[:300]}")
        raise typer.Exit(1)
    body = r.json()
    if json_out:
        sys.stdout.write(json.dumps(body, indent=2, ensure_ascii=False) + "\n")
        return
    avg = body.get("avg_rating")
    avg_s = f"{avg:.2f}/5" if avg is not None else "no ratings"
    console.print(
        f"[bold]{publisher}/{package_id}[/bold]  "
        f"[yellow]{avg_s}[/yellow]  ({body['review_count']} reviews)"
    )
    dist = body.get("distribution", {})
    for star in (5, 4, 3, 2, 1):
        n = dist.get(str(star), dist.get(star, 0))
        bar = "#" * min(int(n), 30)
        console.print(f"  {star}* [dim]{bar}[/dim] {n}")
    if not body["items"]:
        console.print("[dim]No reviews yet.[/dim]")
        return
    table = Table(title=f"Reviews (page {page}/{(body['total'] - 1) // page_size + 1})")
    table.add_column("Rating", style="yellow")
    table.add_column("Author")
    table.add_column("Body")
    table.add_column("Date", style="dim")
    for it in body["items"]:
        table.add_row(
            "*" * it["rating"],
            it.get("user_display_name") or "(anon)",
            (it.get("body") or "")[:80],
            it["created_at"][:10],
        )
    console.print(table)


@hub_cli.command(name="report")
def hub_report(
    target: Annotated[str, typer.Argument(help="<publisher>/<package_id>")],
    reason: Annotated[
        str,
        typer.Option(
            "--reason",
            help="malware | spam | abuse | copyright | broken | other",
        ),
    ] = ...,
    details: Annotated[str | None, typer.Option("--details")] = None,
    daemon: Annotated[str, typer.Option("--daemon", "-d")] = _DEFAULT_DAEMON,
) -> None:
    """Report a package (admin moderation)."""
    publisher, package_id = _split_target(target)
    payload: dict[str, Any] = {"reason": reason}
    if details:
        payload["details"] = details
    r = daemon_request(
        "post",
        f"{daemon}/api/hub/packages/{publisher}/{package_id}/reports",
        json=payload,
    )
    if r.status_code >= 400:
        console.print(f"[red]{r.status_code}:[/red] {r.text[:300]}")
        raise typer.Exit(1)
    out = r.json()
    console.print(
        f"[green]+[/green] Report submitted (id={out['id']}, status={out['status']})"
    )


@hub_cli.command(name="stats")
def hub_stats(
    target: Annotated[str, typer.Argument(help="<publisher>/<package_id>")],
    range_days: Annotated[int, typer.Option("--range", "-r", min=1, max=365)] = 30,
    json_out: Annotated[bool, typer.Option("--json")] = False,
    daemon: Annotated[str, typer.Option("--daemon", "-d")] = _DEFAULT_DAEMON,
) -> None:
    """Show download statistics for a package."""
    publisher, package_id = _split_target(target)
    r = daemon_request(
        "get",
        f"{daemon}/api/hub/packages/{publisher}/{package_id}/stats",
        params={"range": range_days},
    )
    if r.status_code >= 400:
        console.print(f"[red]{r.status_code}:[/red] {r.text[:300]}")
        raise typer.Exit(1)
    body = r.json()
    if json_out:
        sys.stdout.write(json.dumps(body, indent=2, ensure_ascii=False) + "\n")
        return
    console.print(
        Panel.fit(
            f"[bold]{publisher}/{package_id}[/bold]\n"
            f"  range          : last {body['range_days']} days\n"
            f"  total in range : {body['total_downloads_in_range']}\n"
            f"  avg / day      : {body['avg_per_day']}\n"
            f"  by version     : {body['by_version']}",
            title="hub stats",
        )
    )
    series = body.get("series", [])
    if series:
        max_n = max(b["downloads"] for b in series) or 1
        console.print("\n[dim]Daily downloads:[/dim]")
        for b in series[-30:]:
            bar = "#" * int(20 * b["downloads"] / max_n)
            console.print(f"  {b['date']}  [cyan]{bar}[/cyan] {b['downloads']}")


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
        console.print("[red]Invalid target — expected <publisher>/<package_id>[/red]")
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
    console.print(
        f"[green]✓ Installed[/green] [cyan]{out['package_id']}[/cyan]"
        f" v[yellow]{out['version']}[/yellow]"
        f" (scope={out['scope']}, deployed={out.get('deployed', False)})"
    )

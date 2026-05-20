"""CLI commands for database maintenance."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

console = Console()

db_cli = typer.Typer(
    name="db",
    help="Database maintenance and integrity checks.",
    no_args_is_help=True,
)


async def _ensure_engine() -> None:
    """Open a minimal async engine + session factory pointed at the same"""
    from sqlalchemy.ext.asyncio import (
        async_sessionmaker, create_async_engine,
    )
    from digitorn.core.config import get_settings
    from digitorn.core import database as _db

    settings = get_settings()

    is_sqlite = settings.database.url.startswith("sqlite")
    is_asyncpg = "+asyncpg" in settings.database.url
    connect_args: dict = {}
    if is_sqlite:
        connect_args["check_same_thread"] = False
    if is_asyncpg:
        connect_args["statement_cache_size"] = 0
        connect_args["prepared_statement_cache_size"] = 0

    engine = create_async_engine(
        settings.database.url,
        echo=False,
        connect_args=connect_args,
    )
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    _db._engine = engine
    _db._session_factory = factory


@db_cli.command(name="check-seq-monotonic")
def check_seq_monotonic(
    apply_fix: Annotated[
        bool,
        typer.Option("--fix", help="Renumber duplicate seqs (default: dry-run)."),
    ] = False,
) -> None:
    """Find rows in `history_log` that violate per-scope seq uniqueness.

    Two checks:
      * (session_id, seq) duplicates for `kind='event' AND session_id IS NOT NULL`
      * (user_id,    seq) duplicates for `kind='event' AND session_id IS NULL`

    Without `--fix` this is read-only - it lists every duplicate cluster
    so an operator can review before mutating data. With `--fix` the
    older rows of each cluster are renumbered to fresh seqs above the
    scope's current MAX(seq), preserving every event's payload while
    restoring the universal-truth invariant the new UNIQUE INDEX requires.
    """
    asyncio.run(_run_check(apply_fix=apply_fix))


async def _run_check(*, apply_fix: bool) -> None:
    await _ensure_engine()

    from sqlalchemy import text
    from digitorn.core.database import get_session_factory

    sf = get_session_factory()

    session_dups, user_dups = 0, 0
    fixed_session, fixed_user = 0, 0

    async with sf() as db:
        rows = (await db.execute(text(
            """
            SELECT session_id, seq, COUNT(*) AS cnt
              FROM history_log
             WHERE kind = 'event'
               AND session_id IS NOT NULL
             GROUP BY session_id, seq
            HAVING COUNT(*) > 1
             ORDER BY session_id, seq
            """
        ))).fetchall()

        if rows:
            console.print(f"[yellow]Found {len(rows)} session-seq duplicate clusters[/yellow]")
            tbl = Table(title="Session-scope duplicates (sample)")
            tbl.add_column("session_id")
            tbl.add_column("seq", justify="right")
            tbl.add_column("count", justify="right")
            for r in rows[:25]:
                tbl.add_row(r.session_id, str(r.seq), str(r.cnt))
            console.print(tbl)
            if len(rows) > 25:
                console.print(f"[dim]... and {len(rows) - 25} more[/dim]")
            session_dups = sum(int(r.cnt) - 1 for r in rows)
        else:
            console.print("[green]No session-seq duplicates.[/green]")

        urows = (await db.execute(text(
            """
            SELECT user_id, seq, COUNT(*) AS cnt
              FROM history_log
             WHERE kind = 'event'
               AND session_id IS NULL
             GROUP BY user_id, seq
            HAVING COUNT(*) > 1
             ORDER BY user_id, seq
            """
        ))).fetchall()

        if urows:
            console.print(f"[yellow]Found {len(urows)} user-seq duplicate clusters[/yellow]")
            tbl = Table(title="User-scope duplicates (sample)")
            tbl.add_column("user_id")
            tbl.add_column("seq", justify="right")
            tbl.add_column("count", justify="right")
            for r in urows[:25]:
                tbl.add_row(r.user_id, str(r.seq), str(r.cnt))
            console.print(tbl)
            if len(urows) > 25:
                console.print(f"[dim]... and {len(urows) - 25} more[/dim]")
            user_dups = sum(int(r.cnt) - 1 for r in urows)
        else:
            console.print("[green]No user-seq duplicates.[/green]")

        if not apply_fix:
            console.print()
            console.print("[bold]Dry-run summary:[/bold]")
            console.print(f"  session-scope: {session_dups} row(s) would be renumbered")
            console.print(f"  user-scope:    {user_dups} row(s) would be renumbered")
            console.print()
            console.print("Re-run with [cyan]--fix[/cyan] to apply.")
            return


        if rows:
            console.print()
            console.print("[bold]Fixing session-scope duplicates...[/bold]")
            for r in rows:
                sid = r.session_id
                # Per-session current MAX so we know where to start.
                max_row = (await db.execute(text(
                    "SELECT COALESCE(MAX(seq), 0) AS m "
                    "FROM history_log "
                    "WHERE kind='event' AND session_id = :sid"
                ), {"sid": sid})).first()
                cursor = int(max_row.m if max_row else 0)
                # Pick the ROW IDs in the cluster, oldest first.
                clust = (await db.execute(text(
                    "SELECT id FROM history_log "
                    "WHERE kind='event' AND session_id = :sid AND seq = :seq "
                    "ORDER BY ts ASC"
                ), {"sid": sid, "seq": int(r.seq)})).fetchall()
                if len(clust) <= 1:
                    continue
                # Keep the first, renumber the rest.
                for row_id_obj in clust[1:]:
                    cursor += 1
                    await db.execute(text(
                        "UPDATE history_log SET seq = :new_seq WHERE id = :rid"
                    ), {"new_seq": cursor, "rid": row_id_obj.id})
                    fixed_session += 1
            await db.commit()
            console.print(f"[green]Renumbered {fixed_session} session-scope row(s).[/green]")

        if urows:
            console.print()
            console.print("[bold]Fixing user-scope duplicates...[/bold]")
            for r in urows:
                uid = r.user_id
                max_row = (await db.execute(text(
                    "SELECT COALESCE(MAX(seq), 0) AS m "
                    "FROM history_log "
                    "WHERE kind='event' AND session_id IS NULL AND user_id = :uid"
                ), {"uid": uid})).first()
                cursor = int(max_row.m if max_row else 0)
                clust = (await db.execute(text(
                    "SELECT id FROM history_log "
                    "WHERE kind='event' AND session_id IS NULL "
                    "  AND user_id = :uid AND seq = :seq "
                    "ORDER BY ts ASC"
                ), {"uid": uid, "seq": int(r.seq)})).fetchall()
                if len(clust) <= 1:
                    continue
                for row_id_obj in clust[1:]:
                    cursor += 1
                    await db.execute(text(
                        "UPDATE history_log SET seq = :new_seq WHERE id = :rid"
                    ), {"new_seq": cursor, "rid": row_id_obj.id})
                    fixed_user += 1
            await db.commit()
            console.print(f"[green]Renumbered {fixed_user} user-scope row(s).[/green]")

        console.print()
        console.print("[bold green]Cleanup complete.[/bold green]")
        console.print(
            "Restart the daemon so the in-memory seq counters "
            "re-seed from the now-canonical MAX(seq).",
        )


@db_cli.command(name="cleanup-seq-dups")
def cleanup_seq_dups(
    apply_fix: Annotated[
        bool,
        typer.Option("--fix", help="Apply the cleanup (default: dry-run)."),
    ] = False,
) -> None:
    """Alias for `check-seq-monotonic` - renumber duplicate seqs in
    `history_log`. Run with `--fix` to apply, otherwise dry-run.
    """
    asyncio.run(_run_check(apply_fix=apply_fix))

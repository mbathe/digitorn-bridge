"""Hub admin CLI - `python -m digitorn_hub.admin <command>`.

Bootstraps the bridge in three commands:

  - `daemon keygen --out central` writes `central.sk` + `central.pub`
    (raw ed25519, base64-encoded). Hand the .sk to the daemon, keep
    the .pub here to register.
  - `daemon register --name central --pubkey-file central.pub` inserts
    the row into `trusted_daemons`.
  - `daemon revoke --name central` flags the row (subsequent bridge
    calls return 401 instantly).

Kept tiny on purpose - no Click/Typer, just argparse - so the CLI
stays available even in the prod container without optional deps.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make stdout/stderr swallow non-ASCII chars (emojis in package names)
# instead of crashing on Windows cp1252 consoles. Has no effect when
# the console already speaks UTF-8 (Linux, modern Powershell).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from sqlalchemy import select, update

from .db import session_scope
from .models import TrustedDaemon


# ─── Commands ───────────────────────────────────────────────────────


def cmd_keygen(args: argparse.Namespace) -> int:
    out = Path(args.out)
    sk_path = out.with_suffix(".sk")
    pk_path = out.with_suffix(".pub")
    if sk_path.exists() and not args.force:
        print(f"refusing to overwrite {sk_path} (pass --force)", file=sys.stderr)
        return 2

    sk = Ed25519PrivateKey.generate()
    sk_raw = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pk_raw = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    sk_path.write_text(base64.b64encode(sk_raw).decode("ascii") + "\n")
    pk_path.write_text(base64.b64encode(pk_raw).decode("ascii") + "\n")
    sk_path.chmod(0o600)
    print(f"wrote {sk_path} (mode 0600) and {pk_path}")
    return 0


async def _register(name: str, pubkey_b64: str, notes: str | None) -> int:
    async with session_scope() as session:
        existing = (
            await session.execute(
                select(TrustedDaemon).where(TrustedDaemon.name == name)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.revoked_at is not None:
                # Re-issue: keep the row, swap key + clear revocation.
                existing.public_key = pubkey_b64
                existing.notes = notes
                existing.revoked_at = None
                print(f"re-registered {name}")
                return 0
            print(
                f"daemon {name!r} already registered (revoke first if you "
                "want to rotate)",
                file=sys.stderr,
            )
            return 2
        session.add(
            TrustedDaemon(name=name, public_key=pubkey_b64, notes=notes)
        )
    print(f"registered daemon {name!r}")
    return 0


def cmd_register(args: argparse.Namespace) -> int:
    pubkey = Path(args.pubkey_file).read_text().strip()
    return asyncio.run(_register(args.name, pubkey, args.notes))


async def _revoke(name: str) -> int:
    async with session_scope() as session:
        result = await session.execute(
            update(TrustedDaemon)
            .where(TrustedDaemon.name == name, TrustedDaemon.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
    if result.rowcount == 0:
        print(f"no active daemon named {name!r}", file=sys.stderr)
        return 2
    print(f"revoked daemon {name!r}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    return asyncio.run(_revoke(args.name))


async def _list() -> int:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(TrustedDaemon).order_by(TrustedDaemon.created_at)
            )
        ).scalars().all()
    if not rows:
        print("(no trusted daemons registered)")
        return 0
    fmt = "{:<20} {:<6} {:<26} {:<26}"
    print(fmt.format("NAME", "STATE", "CREATED", "LAST USED"))
    for r in rows:
        state = "revoked" if r.revoked_at else "active"
        last = r.last_used_at.isoformat() if r.last_used_at else "-"
        print(fmt.format(r.name, state, r.created_at.isoformat(), last))
    return 0


def cmd_list(_: argparse.Namespace) -> int:
    return asyncio.run(_list())


# ─── packages backfill-icons ───────────────────────────────────────


async def _backfill_icons(dry_run: bool) -> int:
    """Re-extract icons from already-published archives, push to the
    private S3 prefix, repoint icon_url at our `/icon` streaming
    route. Idempotent: rows whose icon_url is already absolute
    (http(s)://) are skipped if they target this Hub, otherwise
    refreshed."""
    from .archive import parse_and_validate
    from .models import Package, PackageVersion, Publisher
    from .storage.s3 import get_storage
    from .routers.packages import _maybe_push_icon

    storage = get_storage()
    fixed = skipped = failed = 0
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Package, Publisher, PackageVersion)
                .join(Publisher, Publisher.id == Package.publisher_id)
                .join(
                    PackageVersion,
                    PackageVersion.id == Package.latest_version_id,
                )
            )
        ).all()

        for pkg, pub, pv in rows:
            current = pkg.icon_url or ""

            # Pull the archive from S3 and re-extract.
            try:
                archive_bytes = await storage.get_object_bytes(pv.archive_object_key)
            except Exception as exc:  # noqa: BLE001
                print(f"  [FAIL] {pub.slug}/{pkg.package_id}: download {exc}")
                failed += 1
                continue
            try:
                parsed = parse_and_validate(archive_bytes)
            except Exception as exc:  # noqa: BLE001
                print(f"  [FAIL] {pub.slug}/{pkg.package_id}: parse {exc}")
                failed += 1
                continue

            if parsed.icon_bytes is None:
                # Either the archive has no icon file, or icon_url is
                # an absolute publisher-hosted URL we should respect.
                if parsed.icon_url:
                    if current != parsed.icon_url and not dry_run:
                        pkg.icon_url = parsed.icon_url
                        pkg.icon_storage_ext = None
                        print(f"  [URL]  {pub.slug}/{pkg.package_id} → {parsed.icon_url}")
                        fixed += 1
                    else:
                        skipped += 1
                else:
                    print(
                        f"  [skip] {pub.slug}/{pkg.package_id}: no icon "
                        f"in archive (was {current!r})"
                    )
                    if not dry_run and (current or pkg.icon_storage_ext):
                        pkg.icon_url = None
                        pkg.icon_storage_ext = None
                    skipped += 1
                continue

            if dry_run:
                print(
                    f"  [DRY ] {pub.slug}/{pkg.package_id}: would push "
                    f"{len(parsed.icon_bytes)} B"
                )
                fixed += 1
                continue

            pushed = await _maybe_push_icon(pub.slug, pkg.package_id, parsed, storage)
            if pushed is None:
                print(f"  [FAIL] {pub.slug}/{pkg.package_id}: push failed")
                failed += 1
                continue
            url, ext = pushed
            pkg.icon_url, pkg.icon_storage_ext = url, ext
            print(f"  [OK]   {pub.slug}/{pkg.package_id} → {url}")
            fixed += 1

    print(f"\n{fixed} fixed, {skipped} skipped, {failed} failed")
    return 0 if failed == 0 else 2


def cmd_backfill_icons(args: argparse.Namespace) -> int:
    return asyncio.run(_backfill_icons(dry_run=args.dry_run))


# ─── Argparse wiring ────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m digitorn_hub.admin",
        description="Hub admin operations.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    daemon = sub.add_parser("daemon", help="Manage trusted central daemons.")
    daemon_sub = daemon.add_subparsers(dest="daemon_cmd", required=True)

    p_keygen = daemon_sub.add_parser("keygen", help="Generate an ed25519 keypair.")
    p_keygen.add_argument(
        "--out",
        required=True,
        help="Output prefix; writes <out>.sk + <out>.pub",
    )
    p_keygen.add_argument("--force", action="store_true")
    p_keygen.set_defaults(func=cmd_keygen)

    p_reg = daemon_sub.add_parser("register", help="Register a trusted daemon.")
    p_reg.add_argument("--name", required=True)
    p_reg.add_argument(
        "--pubkey-file",
        required=True,
        help="Path to a base64-encoded ed25519 public key file.",
    )
    p_reg.add_argument("--notes", default=None)
    p_reg.set_defaults(func=cmd_register)

    p_rev = daemon_sub.add_parser("revoke", help="Revoke a trusted daemon.")
    p_rev.add_argument("--name", required=True)
    p_rev.set_defaults(func=cmd_revoke)

    p_ls = daemon_sub.add_parser("list", help="List trusted daemons.")
    p_ls.set_defaults(func=cmd_list)

    pkgs = sub.add_parser("packages", help="Package admin operations.")
    pkgs_sub = pkgs.add_subparsers(dest="packages_cmd", required=True)

    p_back = pkgs_sub.add_parser(
        "backfill-icons",
        help="Re-extract icons from existing archives, push to public S3, "
        "rewrite icon_url. Idempotent.",
    )
    p_back.add_argument("--dry-run", action="store_true")
    p_back.set_defaults(func=cmd_backfill_icons)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

"""One-shot export of ``packages/digitorn/modules/mcp/catalog.py`` to a
JSON seed file consumed by ``hub.POST /api/v1/mcp/featured/seed``.

Usage::

    py -3.12 tools/export_mcp_catalog_seed.py

Writes ``packages/hub/data/mcp_catalog_seed.json``. The file is
hand-checkable JSON, versioned in the repo, used to bootstrap the
``hub.mcp_featured_entries`` Postgres table on first deploy.

The daemon's ``catalog.py`` is intentionally kept as a last-resort
offline fallback. The authoritative source after Hub deploy is the
Postgres table.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "digitorn"))

from digitorn.modules.mcp.catalog import CATALOG, CatalogEntry  # noqa: E402


OUT_PATH = (
    REPO_ROOT
    / "packages" / "hub" / "src" / "digitorn_hub" / "data"
    / "mcp_catalog_seed.json"
)


def _entry_to_row(entry: CatalogEntry) -> dict:
    """Translate a ``CatalogEntry`` dataclass to the Hub row shape.

    Tuples → lists. Empty strings → omitted. None preserved for nullable
    columns. ``smithery_slug``, ``binary_name`` etc. preserved verbatim.
    """
    row: dict = {}
    for f in fields(entry):
        val = getattr(entry, f.name)
        if isinstance(val, tuple):
            val = list(val)
        if val == "" or val == [] or val == {} or val is None:
            continue
        row[f.name] = val
    return row


def main() -> int:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows = [_entry_to_row(e) for e in CATALOG.values()]

    OUT_PATH.write_text(
        json.dumps({"version": 1, "entries": rows}, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)} ({len(rows)} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

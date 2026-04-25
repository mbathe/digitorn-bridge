"""build.py — ingest the 3 knowledge_base/ collections into the RAG.

This is the bridge between the *files* in ``knowledge_base/`` and the
*Qdrant store* the App Builder agent reads from at runtime. It does
NOT talk to the daemon — it bootstraps a ``RagModule`` directly and
calls its actions in-process. Run it once after every change to the
``knowledge_base/`` content.

Layout it ingests::

    knowledge_base/
    ├── concepts/   ──────► KB "digitorn_concepts"
    ├── modules/    ──────► KB "digitorn_modules"
    └── examples/   ──────► KB "digitorn_examples"

Persistence target::

    ./.digitorn/knowledge_base/.qdrant/

This path matches the ``backend.path`` declared in
``apps/builder/app.yaml`` so the builder agent at runtime sees the
exact same on-disk store this script wrote.

Usage::

    py -3.12 knowledge_base/build.py             # rebuild all 3 collections
    py -3.12 knowledge_base/build.py concepts    # rebuild only one
    py -3.12 knowledge_base/build.py --no-drop   # incremental: keep existing data

By default the script DROPS each KB before ingesting so the result is
fully reproducible from the files on disk. Pass ``--no-drop`` if you
want incremental updates instead (useful while iterating on a few
files without re-embedding everything).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KB_ROOT = REPO_ROOT / "knowledge_base"
PACKAGES_DIR = REPO_ROOT / "packages"
QDRANT_PATH = REPO_ROOT / ".digitorn" / "knowledge_base" / ".qdrant"

# Force UTF-8 + quiet logger
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
logging.basicConfig(level=logging.WARNING)
logging.getLogger("digitorn").setLevel(logging.WARNING)


# ────────────────────────────────────────────────────────────────────
# Collection definitions
# ────────────────────────────────────────────────────────────────────

# (collection_name, source_subdir, file_extensions, description)
COLLECTIONS: list[tuple[str, str, list[str], str]] = [
    (
        "digitorn_concepts",
        "concepts",
        [".md"],
        "Atomic concept cards distilled from the Digitorn docs.",
    ),
    (
        "digitorn_modules",
        "modules",
        [".md"],
        "Auto-generated cards — one per @action method of every loaded module.",
    ),
    (
        "digitorn_examples",
        "examples",
        [".yaml", ".yml"],
        "Curated starter templates that compile and demonstrate major patterns.",
    ),
]


# ────────────────────────────────────────────────────────────────────
# Bootstrap
# ────────────────────────────────────────────────────────────────────


async def build_module():
    """Instantiate a configured ``RagModule`` and call ``on_start``."""
    sys.path.insert(0, str(PACKAGES_DIR))
    try:
        from digitorn.modules.rag.module import RagModule
    except ImportError as exc:
        raise SystemExit(
            f"[build] cannot import RagModule from {PACKAGES_DIR}\n  → {exc}"
        )

    module = RagModule()
    # Direct on-disk Qdrant — the daemon's builder agent will read the
    # same store via the path declared in apps/builder/app.yaml.
    module._config = {
        "backend": {
            "type": "qdrant",
            "path": str(QDRANT_PATH),
        }
    }
    QDRANT_PATH.mkdir(parents=True, exist_ok=True)

    await module.on_start()
    return module


# ────────────────────────────────────────────────────────────────────
# Per-collection workers
# ────────────────────────────────────────────────────────────────────


async def ingest_collection(
    module,
    *,
    name: str,
    source_subdir: str,
    extensions: list[str],
    description: str,
    drop: bool,
) -> tuple[int, int]:
    """Create the KB (optionally dropping first) and ingest the directory.

    Returns ``(documents, chunks)``.
    """
    from digitorn.modules.rag.params import (
        CreateKnowledgeBaseParams,
        DeleteKnowledgeBaseParams,
        IngestDirectoryParams,
    )

    source_dir = KB_ROOT / source_subdir
    if not source_dir.is_dir():
        print(f"  ! {source_subdir}/ — directory missing, skipping", file=sys.stderr)
        return (0, 0)

    file_count = sum(
        1 for ext in extensions for _ in source_dir.rglob(f"*{ext}")
    )
    if file_count == 0:
        print(
            f"  ! {source_subdir}/ — no {extensions} files found, skipping",
            file=sys.stderr,
        )
        return (0, 0)

    print(f"  → {name}  ({file_count} file(s) in {source_subdir}/)", file=sys.stderr)

    if drop:
        try:
            await module.delete_knowledge_base(
                DeleteKnowledgeBaseParams(name=name)
            )
            print(f"      dropped existing {name!r}", file=sys.stderr)
        except Exception as exc:
            # Not fatal — KB might not exist yet on a first run
            print(f"      (drop skipped: {type(exc).__name__})", file=sys.stderr)

    create_result = await module.create_knowledge_base(
        CreateKnowledgeBaseParams(name=name, description=description)
    )
    if not getattr(create_result, "success", False):
        err = getattr(create_result, "error", None) or "unknown error"
        # Already exists is fine when --no-drop
        if "exist" not in str(err).lower():
            print(f"      create_knowledge_base failed: {err}", file=sys.stderr)
            return (0, 0)

    ingest_result = await module.ingest_directory(
        IngestDirectoryParams(
            knowledge_base=name,
            path=str(source_dir),
            extensions=extensions,
            recursive=True,
            max_files=2000,
        )
    )
    data = getattr(ingest_result, "data", None) or {}
    documents = int(data.get("documents") or data.get("added") or 0)
    chunks = int(data.get("chunks") or 0)
    print(
        f"      ingested {documents} document(s) → {chunks} chunk(s)",
        file=sys.stderr,
    )
    return (documents, chunks)


# ────────────────────────────────────────────────────────────────────
# Driver
# ────────────────────────────────────────────────────────────────────


async def main_async(only: list[str], drop: bool) -> None:
    print(f"[build] knowledge_base root: {KB_ROOT}", file=sys.stderr)
    print(f"[build] qdrant path:         {QDRANT_PATH}", file=sys.stderr)
    print(f"[build] mode:                {'rebuild (drop+create)' if drop else 'incremental (--no-drop)'}", file=sys.stderr)
    print(file=sys.stderr)

    print("[build] bootstrapping RagModule...", file=sys.stderr)
    module = await build_module()
    print("[build] module ready\n", file=sys.stderr)

    targets = COLLECTIONS
    if only:
        wanted = set(only)
        targets = [
            (name, sub, exts, desc)
            for (name, sub, exts, desc) in COLLECTIONS
            if sub in wanted or name in wanted
        ]
        if not targets:
            print(
                f"[build] no collection matches {only!r}. "
                f"Available: {[c[1] for c in COLLECTIONS]}",
                file=sys.stderr,
            )
            sys.exit(1)

    totals = {"documents": 0, "chunks": 0}
    for name, source_subdir, extensions, description in targets:
        docs, chunks = await ingest_collection(
            module,
            name=name,
            source_subdir=source_subdir,
            extensions=extensions,
            description=description,
            drop=drop,
        )
        totals["documents"] += docs
        totals["chunks"] += chunks

    # Persist the rag module's state snapshot via the daemon's
    # JsonStateStore so when the daemon next boots, its lifecycle
    # manager auto-restores the KB metadata (name, doc_count, embedding
    # model, BM25 index, content hashes) and the builder agent can
    # actually query what we just ingested.
    #
    # Without this step, the Qdrant collection on disk is fine but the
    # daemon's RagModule starts with `self._kbs == {}`, treats the KB
    # as missing, and rejects queries with "Knowledge base not found".
    try:
        from digitorn.core.state_store import JsonStateStore
        state_dir = Path.home() / ".digitorn" / "state"
        store = JsonStateStore(state_dir)

        # Merge with whatever was already on disk: when the user only
        # rebuilds one collection (e.g. ``build.py modules``), the
        # in-memory module only knows about that one KB. We must not
        # overwrite the metadata for the OTHER KBs that still exist on
        # disk in Qdrant.
        existing = await store.load("rag") or {}
        existing_kbs = (existing.get("knowledge_bases") or {})

        snapshot = module.state_snapshot()
        new_kbs = (snapshot.get("knowledge_bases") or {})
        merged_kbs = dict(existing_kbs)
        merged_kbs.update(new_kbs)
        snapshot["knowledge_bases"] = merged_kbs

        await store.save("rag", snapshot)
        kb_names = sorted(merged_kbs.keys())
        print(
            f"[build] state saved to {state_dir / 'rag.state.json'} "
            f"(KBs: {kb_names})",
            file=sys.stderr,
        )
    except Exception as exc:
        print(
            f"[build] WARNING — state snapshot save failed: {exc}\n"
            f"        The Qdrant data is on disk, but the daemon won't see "
            f"the KB metadata until you fix this.",
            file=sys.stderr,
        )

    # Best-effort shutdown so file handles flush on Windows
    on_stop = getattr(module, "on_stop", None)
    if on_stop is not None:
        try:
            await on_stop()
        except Exception:
            pass

    print(file=sys.stderr)
    print(
        f"[build] DONE — {totals['documents']} document(s), "
        f"{totals['chunks']} chunk(s) across {len(targets)} collection(s)",
        file=sys.stderr,
    )
    print(f"[build] persisted to: {QDRANT_PATH}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "only",
        nargs="*",
        help=(
            "Optional list of collection names or source subdirs to (re)build. "
            "Defaults to all 3."
        ),
    )
    parser.add_argument(
        "--no-drop",
        dest="drop",
        action="store_false",
        default=True,
        help="Don't drop existing knowledge bases before ingesting (incremental mode)",
    )
    args = parser.parse_args()

    asyncio.run(main_async(only=args.only, drop=args.drop))


if __name__ == "__main__":
    main()

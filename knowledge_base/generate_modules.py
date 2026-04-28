"""Backward-compat wrapper around the new generator framework.

The real logic now lives in ``knowledge_base/generators/modules_gen.py``.
This script remains so old ``py -3.12 knowledge_base/generate_modules.py``
calls keep working - and it simply forwards to the framework's CLI with
``--only modules``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from knowledge_base.generators.base import DocGenerator  # noqa: E402
from knowledge_base.generators.modules_gen import ModulesGenerator  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print cards instead of writing to disk.")
    parser.add_argument("--module", default=None, help="Limit generation to a single module.")
    parser.add_argument("--include-internal", action="store_true", help="Include internal=True actions.")
    parser.add_argument("--check", action="store_true", help="Detect drift; exit 1 on divergence.")
    args = parser.parse_args()

    gen: DocGenerator = ModulesGenerator(
        include_internal=args.include_internal,
        only_module=args.module,
    )

    if args.dry_run:
        docs = gen.generate()
        for path, content in docs.items():
            print(f"\n--- {path.relative_to(REPO_ROOT)} ---")
            print(content)
        return

    if args.check:
        report = gen.check()
        if report.is_clean():
            print(f"[generate_modules --check] OK - KB modules/ matches code ground truth.")
            return
        print(f"[generate_modules --check] DRIFT DETECTED ({report.summary()}):", file=sys.stderr)
        for line in report.detail(REPO_ROOT):
            print(line, file=sys.stderr)
        print(
            "\nFix: run `py -3.12 knowledge_base/generate_modules.py` and commit.",
            file=sys.stderr,
        )
        sys.exit(1)

    written, removed = gen.write()
    print(f"[generate_modules] wrote {written} card(s), removed {removed} phantom(s)")


if __name__ == "__main__":
    main()

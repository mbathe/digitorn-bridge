"""run_all.py - CLI entry point for every KB generator.

Usage::

    py -3.12 knowledge_base/generators/run_all.py           # write all
    py -3.12 knowledge_base/generators/run_all.py --check   # detect drift, exit 1 on divergence
    py -3.12 knowledge_base/generators/run_all.py --only schema

Returns a non-zero exit code if any generator reports drift in
``--check`` mode, so it can be used as a CI gate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Force UTF-8 on stdout/stderr - Windows cp1252 default can't encode
# the ✓/✗ glyphs we print.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from .base import DocGenerator
from .hooks_gen import HooksGenerator
from .module_concepts_gen import ModuleConceptsGenerator
from .modules_gen import ModulesGenerator
from .schema_gen import SchemaGenerator

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

GENERATORS: list[type[DocGenerator]] = [
    ModulesGenerator,
    ModuleConceptsGenerator,
    SchemaGenerator,
    HooksGenerator,
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="CI mode - fail if any doc is out of date")
    parser.add_argument("--only", default=None, help="Run only the named generator")
    args = parser.parse_args()

    selected: list[type[DocGenerator]] = []
    for cls in GENERATORS:
        if args.only and cls.name != args.only:
            continue
        selected.append(cls)

    if args.only and not selected:
        available = ", ".join(c.name for c in GENERATORS)
        print(f"[run_all] no generator named {args.only!r}. Available: {available}", file=sys.stderr)
        sys.exit(2)

    any_drift = False
    for cls in selected:
        gen = cls()
        if args.check:
            report = gen.check()
            if report.is_clean():
                print(f"  ✓ {gen.name:<12} clean")
            else:
                print(f"  ✗ {gen.name:<12} {report.summary()}")
                for line in report.detail(REPO_ROOT):
                    print(line)
                any_drift = True
        else:
            written, removed = gen.write()
            print(f"  ✓ {gen.name:<12} wrote {written} file(s), removed {removed} phantom(s)")

    if any_drift:
        print(
            "\n[run_all --check] DRIFT DETECTED.\n"
            "Fix: run `py -3.12 knowledge_base/generators/run_all.py` and commit.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.check:
        print("\n[run_all --check] OK - KB matches code ground truth.")


if __name__ == "__main__":
    main()

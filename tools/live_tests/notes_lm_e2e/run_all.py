"""Run every phase in sequence and produce a single verdict.

Usage::

    py -3.12 tools/live_tests/notes_lm_e2e/run_all.py

Each phase exits 0 (PASS) or 1 (FAIL). If a phase FAILs, we KEEP
running the rest - they're independent sessions so a P3 fail doesn't
poison P4. At the end we print a roll-up.

If the daemon dies mid-run (it has happened) the affected phase will
report its own POST-failure errors. The rest is restartable.
"""
from __future__ import annotations

import importlib
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PHASES = [
    ("P1 setup", "p1_setup"),
    ("P2 identity", "p2_identity"),
    ("P3 ingestion", "p3_ingestion"),
    ("P4 citations", "p4_citations"),
    ("P5 artefacts", "p5_artefacts"),
    ("P6 forms", "p6_forms"),
    ("P7 reload", "p7_reload"),
    ("P8 coherence", "p8_coherence"),
]


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    results: list[tuple[str, int, float]] = []
    t_total = time.monotonic()
    for label, modname in PHASES:
        print()
        print("#" * 72)
        print(f"# {label}")
        print("#" * 72)
        t0 = time.monotonic()
        try:
            mod = importlib.import_module(modname)
            rc = mod.run()
        except SystemExit as exc:
            rc = int(exc.code or 0)
        except Exception as exc:
            print(f"[FATAL] phase {label} crashed: {exc!r}")
            rc = 2
        results.append((label, rc, time.monotonic() - t0))

    elapsed = time.monotonic() - t_total
    print()
    print("=" * 72)
    print("OVERALL VERDICT")
    print("=" * 72)
    n_pass = 0
    n_fail = 0
    for label, rc, dt in results:
        status = "PASS" if rc == 0 else ("FAIL" if rc == 1 else "CRASH")
        if rc == 0:
            n_pass += 1
        else:
            n_fail += 1
        print(f"  {status:5}  {label:18}  {dt:6.1f}s")
    print()
    print(f"Total: {n_pass}/{len(results)} phases PASS in {elapsed/60:.1f}min")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

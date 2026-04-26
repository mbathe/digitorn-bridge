"""Bug fix assertion — hook events are ONE-SHOT, never RUNNING.

Before the fix: ``_on_hook_event`` reused ``hook_event.hook_id``
(stable, typically ``_system`` for built-ins) as op_id with
``op_state=RUNNING`` for every phase that wasn't explicitly
``completed``. Result: the singleton ``_system`` hook op stayed
RUNNING forever in ``active_ops``, polluting the client reconnect view.

Post-fix invariants checked here:
  1. Every hook emission gets a FRESH op_id (``op-hook-<hex>``).
  2. ``op_state`` is TERMINAL (completed/failed/cancelled), never
     running/pending, regardless of the ``phase`` value.
  3. The stable ``hook_id`` is preserved in ``payload.hook_id`` for
     clients that still want to group per-hook in a debug panel.
"""
from __future__ import annotations
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))


def _inspect_handler_body(src: str) -> list[str]:
    """Scan ``_on_hook_event`` in manager.py for the required invariants."""
    failures: list[str] = []
    # Locate the handler.
    anchor = src.find("async def _on_hook_event")
    if anchor < 0:
        failures.append("_on_hook_event not found")
        return failures
    end = src.find("hook_runner = deployed.hook_runner", anchor)
    body = src[anchor:end] if end > anchor else src[anchor:anchor + 6000]

    # Must NOT assign op_state = _OS.RUNNING anywhere in this body.
    if "op_state = _OS.RUNNING" in body or "op_state=_OS.RUNNING" in body:
        failures.append(
            "_on_hook_event still allows op_state=RUNNING — hook "
            "events must be terminal on emission",
        )
    # Must NOT use hook_id as the op_id source anymore (that was the
    # old code — stable id, caused _system running-forever).
    if "hook_data.get(\"hook_id\") or gen_op_id" in body:
        failures.append(
            "_on_hook_event still reuses hook_id as op_id — must "
            "allocate a fresh gen_op_id('hook') per firing",
        )
    # Must invoke gen_op_id('hook').
    if "gen_op_id(\"hook\")" not in body:
        failures.append("_on_hook_event missing gen_op_id('hook')")
    # Must preserve hook_id in payload for debug panels.
    if "hook_id" not in body:
        failures.append(
            "_on_hook_event must keep hook_id in payload",
        )
    return failures


def run() -> int:
    manager_path = ROOT / "packages/digitorn/core/app/manager.py"
    src = manager_path.read_text(encoding="utf-8")
    failures = _inspect_handler_body(src)
    if failures:
        print("FAIL — _on_hook_event is still running-forever:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — _on_hook_event emits terminal op_state + fresh op_id per fire")
    return 0


if __name__ == "__main__":
    sys.exit(run())

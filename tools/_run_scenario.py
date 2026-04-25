"""Runner for one scenario with immediate file output."""
import sys
from pathlib import Path

OUT = Path(__file__).parent / "_scenario_result.txt"

def log(msg):
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")
        f.flush()

OUT.write_text("", encoding="utf-8")
log(f"START {sys.argv[1:]}")

try:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent / "live_tests"))
    from queue_scenarios import (
        scenario_single_turn, scenario_queue_burst,
        scenario_persistent_log_contract, scenario_cross_session_isolation,
    )
    from digitorn.testing import DevClient
    c = DevClient()
    name = sys.argv[1] if len(sys.argv) > 1 else "single_turn"
    fn = {
        "single_turn": scenario_single_turn,
        "queue_burst": scenario_queue_burst,
        "persistent_log": scenario_persistent_log_contract,
        "cross_session": scenario_cross_session_isolation,
    }.get(name)
    if not fn:
        log(f"UNKNOWN scenario: {name}")
        sys.exit(2)
    ok, detail, art = fn(c, "digitorn-chat")
    log(f"RESULT: {'PASS' if ok else 'FAIL'}")
    log(detail)
    log(f"artifacts: {art}")
except Exception as e:
    import traceback
    log(f"EXCEPTION: {type(e).__name__}: {e}")
    log(traceback.format_exc())

log("END")

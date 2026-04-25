"""Single Edit call, multi-line bug. DeepSeek must rewrite an entire
buggy method body in ONE Edit call.

Verification: import the file afterwards and run real scenarios.
The logic must be correct on ALL edge cases — not just "the text changed".
"""
from __future__ import annotations

import io
import sys
import time
import importlib.util
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from digitorn.testing.client import DevClient

TARGET = ROOT / "tests" / "live" / "sandbox" / "complex_bug.py"
SNAPSHOT = ROOT / "tests" / "live" / "sandbox" / "complex_bug_ORIGINAL.py"


def reset():
    TARGET.write_text(SNAPSHOT.read_text(encoding="utf-8"), encoding="utf-8")


def load_module():
    """Dynamically import the fixed file."""
    spec = importlib.util.spec_from_file_location("complex_bug", TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Scenarios that MUST pass after the fix ──
SCENARIOS = [
    # (name, item_specs, coupon_kwargs, expected_discount)
    (
        "no coupon → 0",
        [("A", 2, 50.0, False, False)],
        None,
        0.0,
    ),
    (
        "percent only",
        [("A", 2, 50.0, False, False)],  # subtotal = 100
        dict(code="P10", percent_off=10.0),
        10.0,  # 10% of 100
    ),
    (
        "fixed only",
        [("A", 2, 50.0, False, False)],  # subtotal = 100
        dict(code="F5", fixed_off=5.0),
        5.0,
    ),
    (
        "percent + fixed stack",
        [("A", 2, 50.0, False, False)],  # subtotal = 100
        dict(code="COMBO", percent_off=10.0, fixed_off=5.0),
        15.0,  # 10 + 5
    ),
    (
        "min_cart below threshold → 0",
        [("A", 1, 20.0, False, False)],  # subtotal = 20
        dict(code="MIN50", percent_off=10.0, min_cart_value=50.0),
        0.0,  # below threshold
    ),
    (
        "min_cart at threshold → applies",
        [("A", 1, 50.0, False, False)],  # subtotal = 50 (>= 50)
        dict(code="MIN50", percent_off=10.0, min_cart_value=50.0),
        5.0,  # 10% of 50
    ),
    (
        "giftcards excluded from eligible",
        [("A", 1, 50.0, False, False), ("GC", 1, 30.0, False, True)],  # total=80, eligible=50
        dict(code="P20", percent_off=20.0),
        10.0,  # 20% of 50 (not 80)
    ),
    (
        "digital excluded",
        [("A", 1, 50.0, False, False), ("D", 1, 20.0, True, False)],  # total=70, eligible=50
        dict(code="NODIG", percent_off=10.0, excludes_digital=True),
        5.0,  # 10% of 50
    ),
    (
        "discount capped at eligible subtotal (fixed > subtotal)",
        [("A", 1, 10.0, False, False)],  # subtotal = 10
        dict(code="BIG", fixed_off=50.0),
        10.0,  # capped at 10, not 50
    ),
    (
        "no eligible items → 0",
        [("GC", 1, 30.0, False, True)],  # only giftcard, excluded
        dict(code="P50", percent_off=50.0),
        0.0,  # eligible is 0, discount stays 0
    ),
]


def run_scenarios(mod) -> list[tuple[str, bool, str]]:
    results = []
    Cart = mod.ShoppingCart
    Item = mod.CartItem
    Coupon = mod.Coupon
    for name, items, coupon_kw, expected in SCENARIOS:
        try:
            c = Cart()
            for sku, q, price, is_dig, is_gc in items:
                c.add_item(Item(sku=sku, quantity=q, unit_price=price,
                                is_digital=is_dig, is_giftcard=is_gc))
            if coupon_kw:
                c.apply_coupon(Coupon(**coupon_kw))
            got = c.calculate_discount()
            if abs(got - expected) < 0.001:
                results.append((name, True, f"got {got}"))
            else:
                results.append((name, False, f"expected {expected}, got {got}"))
        except Exception as e:
            results.append((name, False, f"CRASH: {type(e).__name__}: {e}"))
    return results


def main():
    reset()
    print("=" * 70)
    print("  SINGLE-CALL EDIT — DeepSeek rewrites a buggy method in ONE Edit")
    print("=" * 70)
    print(f"Target: {TARGET.relative_to(ROOT)}")
    print(f"Scenarios: {len(SCENARIOS)} (covers 6 sub-bugs — all must pass)")
    print()

    client = DevClient(daemon_url="http://127.0.0.1:8000", auto_approve=True, timeout=300)

    print("Deploying fs-deepseek...")
    app = client.deploy(
        ROOT / "tests" / "live" / "apps" / "fs-deepseek.yaml",
        force=True, wait=5,
    )
    print(f"  status={app.status}")

    session = client.create_session("fs-deepseek", workspace=str(ROOT))

    message = """The file tests/live/sandbox/complex_bug.py has ONE method — `ShoppingCart.calculate_discount` — with SIX interdependent bugs inside its body (bugs A-F listed in the method docstring). The method body (from `eligible = self.subtotal()` down to `return discount`) is fully buggy and must be REWRITTEN in a SINGLE Edit call.

CORRECT BEHAVIOR (per the docstring):
1. If self.coupon is None, return 0.0.
2. Compute ELIGIBLE subtotal: iterate items, skip items where:
   - coupon.excludes_digital and item.is_digital
   - coupon.excludes_giftcards and item.is_giftcard
   Sum the remaining (quantity * unit_price).
3. If eligible < coupon.min_cart_value: return 0.0.
4. discount = (eligible * coupon.percent_off / 100) + coupon.fixed_off  (stack both).
5. Cap: if discount > eligible, discount = eligible.
6. Clamp: if discount < 0, discount = 0.
7. Return discount.

INSTRUCTIONS:
- First Read tests/live/sandbox/complex_bug.py to see the EXACT current body.
- Then make ONE Edit call where old_string is the ENTIRE BUGGY BODY (from `eligible = self.subtotal()` through `return discount` INCLUDED), and new_string is the CORRECT body following the rules above.
- Use ONE Edit call only — do NOT split into multiple edits.
- Preserve the docstring above the body (do not edit the docstring, only the code below it)."""

    print("\nSending to DeepSeek...\n")
    t0 = time.monotonic()
    result = client.send(session, message, timeout=280)
    duration = time.monotonic() - t0
    print(f"Duration: {duration:.0f}s")
    print(f"Tool calls: {len(result.tool_calls)}")
    for tc in result.tool_calls:
        args_short = str(tc.arguments)[:80]
        print(f"  -> {tc.name}({args_short})")

    edit_calls = [tc for tc in result.tool_calls if "edit" in tc.name.lower()]
    print(f"\n  Edit calls: {len(edit_calls)} (target: 1)")
    if len(edit_calls) == 1:
        ec = edit_calls[0]
        old_len = len(str(ec.arguments.get("old_string", "") if isinstance(ec.arguments, dict) else ""))
        new_len = len(str(ec.arguments.get("new_string", "") if isinstance(ec.arguments, dict) else ""))
        print(f"  old_string length: {old_len} chars")
        print(f"  new_string length: {new_len} chars")

    # Verify via execution
    print()
    print("=" * 70)
    print("  FUNCTIONAL SCENARIOS (load the file and run it)")
    print("=" * 70)
    try:
        import importlib
        # Invalidate any cached module
        sys.modules.pop("complex_bug", None)
        mod = load_module()
        scenarios = run_scenarios(mod)
        passed = sum(1 for _, ok, _ in scenarios if ok)
        for name, ok, detail in scenarios:
            status = "PASS" if ok else "FAIL"
            print(f"  [{status}] {name:50s} — {detail}")
        print()
        print(f"RESULT: {passed}/{len(scenarios)} scenarios pass  |  Edit calls: {len(edit_calls)}")

        # Success = 1 Edit call + all scenarios pass
        ok_total = len(edit_calls) == 1 and passed == len(scenarios)
        print(f"Overall: {'PASS' if ok_total else 'FAIL'}")
        return 0 if ok_total else 1
    except SyntaxError as e:
        print(f"  SYNTAX ERROR after Edit — file no longer parses: line {e.lineno}: {e.msg}")
        return 1
    except Exception as e:
        print(f"  IMPORT CRASH: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

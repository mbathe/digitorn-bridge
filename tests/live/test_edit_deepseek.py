"""Live test: DeepSeek + 1371-line file + 6 bugs at different locations.

Byte-exact verification of each fix after the agent finishes.
"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from digitorn.testing.client import DevClient

TARGET = ROOT / "tests" / "live" / "sandbox" / "orders_service.py"
SNAPSHOT = ROOT / "tests" / "live" / "sandbox" / "orders_service_ORIGINAL.py"


def reset():
    TARGET.write_text(SNAPSHOT.read_text(encoding="utf-8"), encoding="utf-8")


def check_bug1(content: str):
    """TAX_RATE = 0.02 → 0.20"""
    if "TAX_RATE = 0.20" in content:
        return True, ""
    if "TAX_RATE = 0.2\n" in content:
        return True, ""
    return False, f"TAX_RATE still wrong. Grep: {[l for l in content.split(chr(10)) if 'TAX_RATE =' in l]}"


def check_bug2(content: str):
    """LineItem.subtotal: `quantity * unit_price + discount` → `- discount`"""
    # find subtotal method body
    idx = content.find("def subtotal(self) -> float:")
    if idx == -1:
        return False, "subtotal() method not found"
    body = content[idx:idx + 300]
    if "unit_price - self.discount" in body or "unit_price - self.discount" in body or "unit_price) - self.discount" in body:
        return True, ""
    # Accept any form that subtracts discount
    if "- self.discount" in body or "- discount" in body.replace("self.discount", "discount"):
        return True, ""
    return False, f"subtotal() still adds discount. Body excerpt: {body[:200]!r}"


def check_bug3(content: str):
    """cancel_order must set order.status = CANCELLED"""
    idx = content.find("def cancel_order(self, order_id: str, reason: str = \"\") -> Order:")
    if idx == -1:
        return False, "cancel_order() not found"
    # body is between this def and the next one
    next_def = content.find("\n    def ", idx + 50)
    body = content[idx:next_def if next_def != -1 else idx + 1500]
    # must assign status to CANCELLED somewhere in the body
    if "order.status = OrderStatus.CANCELLED" in body:
        return True, ""
    if 'order.status = "cancelled"' in body:
        return True, ""
    return False, f"cancel_order() doesn't set order.status = CANCELLED. Body: {body[:400]!r}"


def check_bug4(content: str):
    """idempotency lookup was wrong ('fake_key'). Fix: use idempotency_key"""
    idx = content.find("idempotency_key is checked")
    # Just check that 'fake_key' is gone and idempotency lookup uses the real key
    if "'fake_key'" in content or '"fake_key"' in content:
        return False, "'fake_key' literal still present - lookup still broken"
    # Must look up by the actual idempotency_key somehow
    if "self._transactions.get(idempotency_key)" in content:
        return True, ""
    # Or via a filter comprehension
    if "idempotency_key\"]" in content and 'existing' in content.lower():
        return True, ""
    # Accept a loop iterating transactions
    return "fake_key" not in content, "Still uses 'fake_key'"


def check_bug5(content: str):
    """Notifier.send missing return True"""
    idx = content.find("def send(\n        self,\n        customer: Customer,")
    if idx == -1:
        idx = content.find("def send(")
    next_def = content.find("\n    def ", idx + 50)
    body = content[idx:next_def if next_def != -1 else idx + 2000]
    if "return True" in body:
        return True, ""
    return False, f"Notifier.send() missing 'return True'. Body[:400]: {body[:400]!r}"


def check_bug6(content: str):
    """cancellation_rate returns count, should return count/len(orders)"""
    idx = content.find("def cancellation_rate(self, orders: list[Order]) -> float:")
    if idx == -1:
        return False, "cancellation_rate() not found"
    next_def = content.find("\n    def ", idx + 20)
    body = content[idx:next_def if next_def != -1 else idx + 500]
    # Must have a division
    if "/ len(orders)" in body or "/len(orders)" in body:
        return True, ""
    return False, f"cancellation_rate doesn't divide. Body: {body[:300]!r}"


CHECKS = [
    ("bug1_TAX_RATE_0.20", check_bug1),
    ("bug2_subtotal_subtracts_discount", check_bug2),
    ("bug3_cancel_sets_status", check_bug3),
    ("bug4_idempotency_real_key", check_bug4),
    ("bug5_send_returns_True", check_bug5),
    ("bug6_cancellation_rate_divides", check_bug6),
]


def main():
    reset()
    src_lines = TARGET.read_text(encoding="utf-8").count("\n") + 1
    print("=" * 70)
    print(f"  EDIT HARD TEST - DeepSeek on {src_lines}-line file, 6 bugs")
    print("=" * 70)
    print(f"Target: {TARGET.relative_to(ROOT)}")
    print()

    client = DevClient(daemon_url="http://127.0.0.1:8001", auto_approve=True, timeout=600)

    print("Deploying fs-deepseek...")
    app = client.deploy(
        ROOT / "tests" / "live" / "apps" / "fs-deepseek.yaml",
        force=True, wait=5,
    )
    print(f"  status={app.status}")

    session = client.create_session("fs-deepseek", workspace=str(ROOT))

    message = """The file tests/live/sandbox/orders_service.py (about 1370 lines) has SIX bugs marked with comments `# BUG #N`. Find them and fix each with a surgical Edit call.

Workflow:
1. First call Grep with pattern `# BUG #` and path `tests/live/sandbox/orders_service.py` to find all 6 bug locations at once.
2. Read the file (or targeted sections using offset+limit) to see the exact code around each bug.
3. For EACH bug, call Edit ONCE with a precise old_string that is UNIQUE in the file (add surrounding context if needed).
4. If an Edit fails ("not unique" or "not found"), read the error, widen the context or narrow to exact text, and retry.

The bugs:
- Bug #1: TAX_RATE is 0.02, should be 0.20 (20% tax).
- Bug #2: LineItem.subtotal adds discount, should subtract.
- Bug #3: OrderService.cancel_order forgot to set `order.status = OrderStatus.CANCELLED`.
- Bug #4: PaymentGateway.charge idempotency lookup uses literal `"fake_key"` instead of the real `idempotency_key` parameter.
- Bug #5: Notifier.send is missing `return True` at the end of success path.
- Bug #6: ReportGenerator.cancellation_rate returns the raw count, should divide by `len(orders)`.

Fix all 6. Do NOT rewrite the file - only surgical Edit calls."""

    print(f"\nSending to DeepSeek...\n")
    t0 = time.monotonic()
    result = client.send(session, message, timeout=580)
    duration = time.monotonic() - t0
    print(f"Duration: {duration:.0f}s")
    print(f"Tool calls: {len(result.tool_calls)}")
    edit_count = sum(1 for tc in result.tool_calls if "edit" in tc.name.lower())
    read_count = sum(1 for tc in result.tool_calls if "read" in tc.name.lower())
    grep_count = sum(1 for tc in result.tool_calls if "grep" in tc.name.lower())
    print(f"  Read: {read_count}, Edit: {edit_count}, Grep: {grep_count}")
    print()

    new_content = TARGET.read_text(encoding="utf-8")
    print(f"File after: {new_content.count(chr(10)) + 1} lines, {len(new_content)} chars")
    print()

    # Run the byte-level checks
    print("=" * 70)
    print("  CHECKS")
    print("=" * 70)
    passed = 0
    for name, check in CHECKS:
        ok, reason = check(new_content)
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")
        if not ok:
            print(f"         {reason[:250]}")
        if ok:
            passed += 1

    print()
    print(f"RESULT: {passed}/{len(CHECKS)} bugs fixed in {duration:.0f}s")

    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())

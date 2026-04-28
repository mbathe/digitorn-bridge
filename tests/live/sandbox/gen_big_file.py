"""Generate a realistic ~1500-line Python file with known bugs placed
at specific line numbers. Used by test_edit_deepseek.py.
"""
from pathlib import Path


def build() -> str:
    parts = []

    # HEADER (~30 lines)
    parts.append('''"""Order management service - handles orders, payments, notifications.

A larger module showing how Edit tool scales to real-world files.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Callable

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
BATCH_SIZE = 100
# BUG #1 (line ~26): TAX_RATE should be 0.20 (20%), not 0.02 (2%)
TAX_RATE = 0.02
SHIPPING_THRESHOLD = 50.0
VERSION = "1.0.0"

''')

    # ENUMS / TYPES (~50 lines)
    parts.append('''
# ────────────────────────────────────────────────────────────
# Status definitions
# ────────────────────────────────────────────────────────────


class OrderStatus:
    """Order lifecycle states."""
    PENDING = "pending"
    CONFIRMED = "confirmed"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class PaymentMethod:
    CARD = "card"
    BANK = "bank"
    CRYPTO = "crypto"
    WALLET = "wallet"


class NotificationChannel:
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"


''')

    # DATA MODELS (~150 lines)
    parts.append('''
# ────────────────────────────────────────────────────────────
# Data models
# ────────────────────────────────────────────────────────────


@dataclass
class Address:
    street: str
    city: str
    postal_code: str
    country: str
    state: Optional[str] = None

    def formatted(self) -> str:
        lines = [self.street]
        if self.state:
            lines.append(f"{self.city}, {self.state} {self.postal_code}")
        else:
            lines.append(f"{self.city} {self.postal_code}")
        lines.append(self.country)
        return "\\n".join(lines)

    def is_valid(self) -> bool:
        return bool(self.street and self.city and self.postal_code and self.country)


@dataclass
class Customer:
    customer_id: str
    email: str
    name: str
    phone: Optional[str] = None
    address: Optional[Address] = None
    preferences: dict[str, Any] = field(default_factory=dict)

    def display_name(self) -> str:
        return self.name if self.name else self.email


@dataclass
class LineItem:
    sku: str
    name: str
    quantity: int
    unit_price: float
    discount: float = 0.0

    def subtotal(self) -> float:
        # BUG #2 (line ~115): should subtract discount, not add it
        return self.quantity * self.unit_price + self.discount

    def is_free_item(self) -> bool:
        return self.unit_price == 0.0


@dataclass
class Order:
    order_id: str
    customer: Customer
    items: list[LineItem] = field(default_factory=list)
    status: str = OrderStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    shipping_address: Optional[Address] = None
    payment_method: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def total(self) -> float:
        return sum(item.subtotal() for item in self.items)

    def tax(self) -> float:
        return self.total() * TAX_RATE

    def grand_total(self) -> float:
        return self.total() + self.tax() + self._shipping_cost()

    def _shipping_cost(self) -> float:
        if self.total() >= SHIPPING_THRESHOLD:
            return 0.0
        return 5.0

    def item_count(self) -> int:
        return sum(item.quantity for item in self.items)

''')

    # ORDER SERVICE (~400 lines)
    parts.append('''
# ────────────────────────────────────────────────────────────
# Order service
# ────────────────────────────────────────────────────────────


class OrderService:
    """Handles order creation, updates, and lifecycle."""

    def __init__(self, db, notifier=None, payment_gateway=None):
        self._db = db
        self._notifier = notifier
        self._payment = payment_gateway
        self._orders: dict[str, Order] = {}
        self._stats = {
            "created": 0,
            "confirmed": 0,
            "shipped": 0,
            "cancelled": 0,
        }

    def create_order(self, customer: Customer, items: list[LineItem]) -> Order:
        """Create a new pending order."""
        if not items:
            raise ValueError("Order must have at least one item")
        if not customer.address or not customer.address.is_valid():
            raise ValueError("Customer must have a valid address")

        order = Order(
            order_id=str(uuid.uuid4()),
            customer=customer,
            items=items,
            status=OrderStatus.PENDING,
            shipping_address=customer.address,
        )
        self._orders[order.order_id] = order
        self._stats["created"] += 1
        logger.info("order_created id=%s total=%.2f", order.order_id, order.total())
        return order

    def confirm_order(self, order_id: str, payment_method: str) -> Order:
        """Move an order from pending to confirmed."""
        order = self._orders.get(order_id)
        if not order:
            raise KeyError(f"Order {order_id} not found")
        if order.status != OrderStatus.PENDING:
            raise RuntimeError(f"Cannot confirm order in status {order.status}")

        order.payment_method = payment_method
        order.status = OrderStatus.CONFIRMED
        self._stats["confirmed"] += 1
        logger.info("order_confirmed id=%s", order_id)

        if self._notifier:
            self._notifier.send(
                customer=order.customer,
                subject="Order confirmed",
                body=f"Your order {order_id} is confirmed. Total: {order.grand_total():.2f}",
            )
        return order

    def cancel_order(self, order_id: str, reason: str = "") -> Order:
        """Cancel an order (must still be pending or confirmed)."""
        order = self._orders.get(order_id)
        if not order:
            raise KeyError(f"Order {order_id} not found")
        if order.status in (OrderStatus.SHIPPED, OrderStatus.DELIVERED, OrderStatus.REFUNDED):
            raise RuntimeError(f"Cannot cancel order in status {order.status}")

        # BUG #3 (line ~215): forgot to actually SET the status
        # Only logs + stats, never updates order.status
        self._stats["cancelled"] += 1
        logger.warning("order_cancelled id=%s reason=%s", order_id, reason)
        if self._notifier:
            self._notifier.send(
                customer=order.customer,
                subject="Order cancelled",
                body=f"Your order {order_id} has been cancelled. {reason}",
            )
        return order

    def ship_order(self, order_id: str, tracking_number: str) -> Order:
        """Mark order as shipped with tracking number."""
        order = self._orders.get(order_id)
        if not order:
            raise KeyError(f"Order {order_id} not found")
        if order.status != OrderStatus.CONFIRMED:
            raise RuntimeError(f"Order must be confirmed before shipping, got {order.status}")

        order.status = OrderStatus.SHIPPED
        order.metadata["tracking_number"] = tracking_number
        order.metadata["shipped_at"] = datetime.now(timezone.utc).isoformat()
        self._stats["shipped"] += 1
        logger.info("order_shipped id=%s tracking=%s", order_id, tracking_number)

        if self._notifier:
            self._notifier.send(
                customer=order.customer,
                subject="Order shipped",
                body=f"Your order {order_id} has shipped. Tracking: {tracking_number}",
            )
        return order

    def deliver_order(self, order_id: str) -> Order:
        """Mark order as delivered."""
        order = self._orders.get(order_id)
        if not order:
            raise KeyError(f"Order {order_id} not found")
        if order.status != OrderStatus.SHIPPED:
            raise RuntimeError(f"Order must be shipped before delivery, got {order.status}")
        order.status = OrderStatus.DELIVERED
        order.metadata["delivered_at"] = datetime.now(timezone.utc).isoformat()
        logger.info("order_delivered id=%s", order_id)
        return order

    def refund_order(self, order_id: str, amount: float, reason: str) -> Order:
        """Refund a delivered order."""
        order = self._orders.get(order_id)
        if not order:
            raise KeyError(f"Order {order_id} not found")
        if order.status != OrderStatus.DELIVERED:
            raise RuntimeError(f"Can only refund delivered orders, got {order.status}")
        if amount > order.grand_total():
            raise ValueError(f"Refund amount {amount} exceeds order total {order.grand_total()}")

        if self._payment:
            self._payment.refund(order.payment_method, amount)
        order.status = OrderStatus.REFUNDED
        order.metadata["refund_amount"] = amount
        order.metadata["refund_reason"] = reason
        logger.info("order_refunded id=%s amount=%.2f", order_id, amount)
        return order

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def list_orders(self, status: Optional[str] = None) -> list[Order]:
        orders = list(self._orders.values())
        if status:
            orders = [o for o in orders if o.status == status]
        return sorted(orders, key=lambda o: o.created_at, reverse=True)

    def count_orders(self) -> int:
        return len(self._orders)

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

''')

    # PAYMENT MODULE (~200 lines)
    parts.append('''
# ────────────────────────────────────────────────────────────
# Payment gateway
# ────────────────────────────────────────────────────────────


class PaymentError(Exception):
    pass


class PaymentGateway:
    """Simulated payment gateway."""

    def __init__(self, api_key: str, sandbox: bool = True):
        self.api_key = api_key
        self.sandbox = sandbox
        self._transactions: dict[str, dict[str, Any]] = {}

    def charge(
        self,
        amount: float,
        method: str,
        customer_id: str,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Charge a payment. Returns a transaction ID."""
        if amount <= 0:
            raise PaymentError(f"Invalid amount: {amount}")
        if method not in (PaymentMethod.CARD, PaymentMethod.BANK,
                          PaymentMethod.CRYPTO, PaymentMethod.WALLET):
            raise PaymentError(f"Unsupported method: {method}")

        # BUG #4 (line ~313): idempotency key is checked but the lookup
        # uses the wrong dict - always starts a new transaction
        if idempotency_key:
            existing = self._transactions.get("fake_key")
            if existing:
                return existing["tx_id"]

        tx_id = f"tx_{uuid.uuid4().hex[:12]}"
        self._transactions[tx_id] = {
            "tx_id": tx_id,
            "amount": amount,
            "method": method,
            "customer_id": customer_id,
            "status": "succeeded",
            "idempotency_key": idempotency_key,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("payment_charged tx=%s amount=%.2f", tx_id, amount)
        return tx_id

    def refund(self, method: str, amount: float) -> str:
        """Process a refund."""
        if amount <= 0:
            raise PaymentError(f"Invalid refund amount: {amount}")
        refund_id = f"ref_{uuid.uuid4().hex[:12]}"
        self._transactions[refund_id] = {
            "tx_id": refund_id,
            "type": "refund",
            "amount": amount,
            "method": method,
            "status": "succeeded",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("payment_refunded id=%s amount=%.2f", refund_id, amount)
        return refund_id

    def get_transaction(self, tx_id: str) -> Optional[dict[str, Any]]:
        return self._transactions.get(tx_id)

    def list_transactions(self, customer_id: Optional[str] = None) -> list[dict[str, Any]]:
        txs = list(self._transactions.values())
        if customer_id:
            txs = [t for t in txs if t.get("customer_id") == customer_id]
        return txs

    def validate_card_number(self, number: str) -> bool:
        """Luhn checksum validation."""
        digits = [int(d) for d in number if d.isdigit()]
        if len(digits) < 12:
            return False
        checksum = 0
        for i, d in enumerate(reversed(digits)):
            if i % 2 == 1:
                d *= 2
                if d > 9:
                    d -= 9
            checksum += d
        return checksum % 10 == 0

    def mask_card(self, number: str) -> str:
        if len(number) < 4:
            return "****"
        return "*" * (len(number) - 4) + number[-4:]

''')

    # NOTIFIER (~200 lines)
    parts.append('''
# ────────────────────────────────────────────────────────────
# Notifier
# ────────────────────────────────────────────────────────────


class Notifier:
    """Sends notifications across channels."""

    def __init__(self, channels: Optional[list[str]] = None):
        self.channels = channels or [NotificationChannel.EMAIL]
        self._sent_log: list[dict[str, Any]] = []
        self._retry_queue: list[dict[str, Any]] = []
        self._max_retries = MAX_RETRIES

    def send(
        self,
        customer: Customer,
        subject: str,
        body: str,
        channel: Optional[str] = None,
    ) -> bool:
        """Send a notification. Returns True on success."""
        effective_channel = channel or self.channels[0]
        # BUG #5 (line ~398): missing `return True` - falls through and
        # the function returns None even though the send succeeded
        notification = {
            "customer_id": customer.customer_id,
            "channel": effective_channel,
            "subject": subject,
            "body": body,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "status": "sent",
        }
        self._sent_log.append(notification)
        logger.info(
            "notification_sent customer=%s channel=%s subject=%s",
            customer.customer_id, effective_channel, subject,
        )

    def broadcast(self, customers: list[Customer], subject: str, body: str) -> int:
        """Send to many customers. Returns count of successful sends."""
        count = 0
        for customer in customers:
            if self.send(customer, subject, body):
                count += 1
        return count

    def retry_failed(self) -> int:
        """Retry all failed notifications. Returns count of retries."""
        retried = 0
        for item in list(self._retry_queue):
            attempts = item.get("attempts", 0)
            if attempts >= self._max_retries:
                self._retry_queue.remove(item)
                continue
            try:
                self.send(
                    customer=item["customer"],
                    subject=item["subject"],
                    body=item["body"],
                    channel=item.get("channel"),
                )
                self._retry_queue.remove(item)
                retried += 1
            except Exception:
                item["attempts"] = attempts + 1
        return retried

    def history(self, customer_id: Optional[str] = None) -> list[dict[str, Any]]:
        log = self._sent_log
        if customer_id:
            log = [n for n in log if n.get("customer_id") == customer_id]
        return list(log)

    def clear_log(self) -> None:
        self._sent_log.clear()


''')

    # BACKGROUND WORKER (~150 lines)
    parts.append('''
# ────────────────────────────────────────────────────────────
# Background worker
# ────────────────────────────────────────────────────────────


class BackgroundWorker:
    """Processes async jobs (notifications, reports, etc)."""

    def __init__(self, job_handler: Callable[[dict], Any]):
        self._handler = job_handler
        self._queue: list[dict[str, Any]] = []
        self._done: list[dict[str, Any]] = []
        self._failed: list[dict[str, Any]] = []
        self._running = False

    def enqueue(self, job: dict[str, Any]) -> None:
        job.setdefault("job_id", str(uuid.uuid4()))
        job.setdefault("enqueued_at", datetime.now(timezone.utc).isoformat())
        self._queue.append(job)

    def process_one(self) -> Optional[dict[str, Any]]:
        if not self._queue:
            return None
        job = self._queue.pop(0)
        try:
            result = self._handler(job)
            job["result"] = result
            job["status"] = "done"
            self._done.append(job)
            return job
        except Exception as exc:
            job["error"] = str(exc)
            job["status"] = "failed"
            self._failed.append(job)
            logger.error("job_failed id=%s error=%s", job.get("job_id"), exc)
            return job

    def process_all(self) -> int:
        count = 0
        while self._queue:
            self.process_one()
            count += 1
        return count

    def pending_count(self) -> int:
        return len(self._queue)

    def stats(self) -> dict[str, int]:
        return {
            "pending": len(self._queue),
            "done": len(self._done),
            "failed": len(self._failed),
        }

    def clear_failed(self) -> int:
        n = len(self._failed)
        self._failed.clear()
        return n


''')

    # UTILITIES (~150 lines)
    parts.append('''
# ────────────────────────────────────────────────────────────
# Utilities
# ────────────────────────────────────────────────────────────


def format_money(amount: float, currency: str = "USD") -> str:
    """Format a money amount with currency."""
    symbols = {"USD": "$", "EUR": "€", "GBP": "£"}
    sym = symbols.get(currency, currency + " ")
    return f"{sym}{amount:,.2f}"


def calculate_discount(amount: float, percent: float) -> float:
    """Apply a percentage discount."""
    if percent < 0 or percent > 100:
        raise ValueError(f"Invalid discount percent: {percent}")
    return amount * (1 - percent / 100)


def safe_get(d: dict, path: str, default=None):
    """Get a nested dict value by dot-path."""
    keys = path.split(".")
    current = d
    for k in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(k, default)
        if current is default:
            return default
    return current


def chunks(seq: list, size: int):
    """Yield chunks of size n from a list."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def serialize_order(order: Order) -> dict[str, Any]:
    """Convert an Order to a JSON-safe dict."""
    return {
        "order_id": order.order_id,
        "status": order.status,
        "customer": {
            "id": order.customer.customer_id,
            "email": order.customer.email,
            "name": order.customer.name,
        },
        "items": [
            {
                "sku": i.sku,
                "name": i.name,
                "quantity": i.quantity,
                "unit_price": i.unit_price,
                "discount": i.discount,
            }
            for i in order.items
        ],
        "total": order.total(),
        "tax": order.tax(),
        "grand_total": order.grand_total(),
        "created_at": order.created_at.isoformat(),
    }


def deserialize_order(data: dict[str, Any], customer_store: dict[str, Customer]) -> Order:
    """Reconstruct an Order from its serialized form."""
    customer_id = data["customer"]["id"]
    customer = customer_store.get(customer_id)
    if not customer:
        raise KeyError(f"Customer {customer_id} not found")

    items = [
        LineItem(
            sku=i["sku"],
            name=i["name"],
            quantity=i["quantity"],
            unit_price=i["unit_price"],
            discount=i.get("discount", 0.0),
        )
        for i in data["items"]
    ]
    order = Order(
        order_id=data["order_id"],
        customer=customer,
        items=items,
        status=data.get("status", OrderStatus.PENDING),
    )
    if data.get("created_at"):
        order.created_at = datetime.fromisoformat(data["created_at"])
    return order


''')

    # REPORTING (~200 lines)
    parts.append('''
# ────────────────────────────────────────────────────────────
# Reporting
# ────────────────────────────────────────────────────────────


class ReportGenerator:
    """Generates aggregate reports from order data."""

    def __init__(self, order_service: OrderService):
        self._svc = order_service

    def revenue_by_day(self, orders: list[Order]) -> dict[str, float]:
        revenue: dict[str, float] = {}
        for order in orders:
            if order.status in (OrderStatus.CANCELLED, OrderStatus.REFUNDED):
                continue
            key = order.created_at.strftime("%Y-%m-%d")
            revenue[key] = revenue.get(key, 0.0) + order.grand_total()
        return revenue

    def top_customers(self, orders: list[Order], limit: int = 10) -> list[tuple[str, float]]:
        totals: dict[str, float] = {}
        for order in orders:
            cid = order.customer.customer_id
            totals[cid] = totals.get(cid, 0.0) + order.grand_total()
        sorted_pairs = sorted(totals.items(), key=lambda p: p[1], reverse=True)
        return sorted_pairs[:limit]

    def bestsellers(self, orders: list[Order], limit: int = 10) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for order in orders:
            for item in order.items:
                counts[item.sku] = counts.get(item.sku, 0) + item.quantity
        sorted_pairs = sorted(counts.items(), key=lambda p: p[1], reverse=True)
        return sorted_pairs[:limit]

    def average_order_value(self, orders: list[Order]) -> float:
        if not orders:
            return 0.0
        return sum(o.grand_total() for o in orders) / len(orders)

    def cancellation_rate(self, orders: list[Order]) -> float:
        if not orders:
            return 0.0
        cancelled = sum(1 for o in orders if o.status == OrderStatus.CANCELLED)
        # BUG #6 (line ~651): returns count instead of rate - missing division
        return cancelled

    def summary(self, orders: list[Order]) -> dict[str, Any]:
        return {
            "total_orders": len(orders),
            "avg_order_value": self.average_order_value(orders),
            "cancellation_rate": self.cancellation_rate(orders),
            "revenue_by_day": self.revenue_by_day(orders),
            "top_customers": self.top_customers(orders),
            "bestsellers": self.bestsellers(orders),
        }

    def export_json(self, orders: list[Order], path: str) -> None:
        payload = [serialize_order(o) for o in orders]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)


''')

    # MAIN (~50 lines)
    parts.append('''
# ────────────────────────────────────────────────────────────
# Main entrypoint
# ────────────────────────────────────────────────────────────


def demo() -> None:
    """Run a demo scenario."""
    gateway = PaymentGateway(api_key="demo_key", sandbox=True)
    notifier = Notifier(channels=[NotificationChannel.EMAIL])
    service = OrderService(db=None, notifier=notifier, payment_gateway=gateway)
    reporter = ReportGenerator(service)

    addr = Address(
        street="1 Main St",
        city="Paris",
        postal_code="75001",
        country="France",
    )
    alice = Customer(
        customer_id="cust_001",
        email="alice@example.com",
        name="Alice",
        address=addr,
    )

    items = [
        LineItem(sku="SKU-1", name="Widget", quantity=2, unit_price=19.99),
        LineItem(sku="SKU-2", name="Gadget", quantity=1, unit_price=49.99, discount=5.0),
    ]
    order = service.create_order(alice, items)
    service.confirm_order(order.order_id, PaymentMethod.CARD)
    service.ship_order(order.order_id, "TRACK-123")

    print(f"Order total: {format_money(order.grand_total())}")
    print(f"Stats: {service.stats()}")
    print(f"Summary: {reporter.summary([order])}")


if __name__ == "__main__":
    demo()
''')

    return "".join(parts)


if __name__ == "__main__":
    content = build()
    out = Path(__file__).parent / "orders_service.py"
    out.write_text(content, encoding="utf-8")
    print(f"Wrote {out} ({len(content)} chars, {content.count(chr(10))} lines)")

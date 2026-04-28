"""Order management service - handles orders, payments, notifications.

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
        return "\n".join(lines)

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



# ────────────────────────────────────────────────────────────
# Extra service classes
# ────────────────────────────────────────────────────────────


class InventoryService:
    """Tracks stock levels per SKU."""

    def __init__(self):
        self._stock: dict[str, int] = {}
        self._history: list[dict] = []

    def set_stock(self, sku: str, qty: int) -> None:
        if qty < 0:
            raise ValueError(f"Stock cannot be negative: {qty}")
        self._stock[sku] = qty
        self._history.append({
            "sku": sku, "action": "set", "qty": qty,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    def get_stock(self, sku: str) -> int:
        return self._stock.get(sku, 0)

    def reserve(self, sku: str, qty: int) -> bool:
        current = self.get_stock(sku)
        if current < qty:
            return False
        self._stock[sku] = current - qty
        self._history.append({
            "sku": sku, "action": "reserve", "qty": qty,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        return True

    def release(self, sku: str, qty: int) -> None:
        self._stock[sku] = self.get_stock(sku) + qty
        self._history.append({
            "sku": sku, "action": "release", "qty": qty,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    def low_stock_items(self, threshold: int = 10) -> list[str]:
        return [sku for sku, qty in self._stock.items() if qty < threshold]

    def restock(self, sku: str, qty: int) -> int:
        new_total = self.get_stock(sku) + qty
        self._stock[sku] = new_total
        self._history.append({
            "sku": sku, "action": "restock", "qty": qty,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        return new_total

    def total_items(self) -> int:
        return sum(self._stock.values())

    def history(self, sku: str = None) -> list[dict]:
        if sku:
            return [h for h in self._history if h["sku"] == sku]
        return list(self._history)


class DiscountEngine:
    """Applies promotional rules to orders."""

    def __init__(self):
        self._rules: list[dict] = []

    def add_rule(self, name: str, condition: Callable, discount_pct: float) -> None:
        self._rules.append({
            "name": name, "condition": condition, "discount_pct": discount_pct,
        })

    def apply(self, order: Order) -> float:
        total_discount = 0.0
        for rule in self._rules:
            try:
                if rule["condition"](order):
                    total_discount += order.total() * rule["discount_pct"] / 100
            except Exception as exc:
                logger.warning("rule_error name=%s error=%s", rule["name"], exc)
        return total_discount

    def rules(self) -> list[str]:
        return [r["name"] for r in self._rules]

    def clear(self) -> None:
        self._rules.clear()


class AuditLog:
    """Tamper-evident audit trail."""

    def __init__(self):
        self._entries: list[dict] = []

    def record(self, actor: str, action: str, target: str, metadata: dict = None) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "action": action,
            "target": target,
            "metadata": metadata or {},
        }
        self._entries.append(entry)

    def query(self, actor: str = None, action: str = None) -> list[dict]:
        result = self._entries
        if actor:
            result = [e for e in result if e["actor"] == actor]
        if action:
            result = [e for e in result if e["action"] == action]
        return result

    def count(self) -> int:
        return len(self._entries)

    def export(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._entries, f, indent=2)


class CacheLayer:
    """In-memory TTL cache."""

    def __init__(self, default_ttl: int = 300):
        self._store: dict[str, tuple[Any, float]] = {}
        self._ttl = default_ttl
        self._hits = 0
        self._misses = 0

    def set(self, key: str, value: Any, ttl: int = None) -> None:
        import time as _t
        ttl = ttl or self._ttl
        self._store[key] = (value, _t.time() + ttl)

    def get(self, key: str) -> Optional[Any]:
        import time as _t
        entry = self._store.get(key)
        if not entry:
            self._misses += 1
            return None
        value, expiry = entry
        if _t.time() > expiry:
            self._store.pop(key, None)
            self._misses += 1
            return None
        self._hits += 1
        return value

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    def stats(self) -> dict:
        total = self._hits + self._misses
        hit_rate = self._hits / total if total else 0.0
        return {
            "size": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": hit_rate,
        }


class EventBus:
    """Simple pub/sub event bus."""

    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = {}
        self._event_log: list[dict] = []

    def subscribe(self, event: str, handler: Callable) -> None:
        self._subscribers.setdefault(event, []).append(handler)

    def unsubscribe(self, event: str, handler: Callable) -> bool:
        handlers = self._subscribers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)
            return True
        return False

    def publish(self, event: str, payload: Any) -> int:
        entry = {
            "event": event,
            "payload": payload,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self._event_log.append(entry)
        delivered = 0
        for handler in self._subscribers.get(event, []):
            try:
                handler(payload)
                delivered += 1
            except Exception as exc:
                logger.error("handler_error event=%s error=%s", event, exc)
        return delivered

    def recent_events(self, limit: int = 50) -> list[dict]:
        return self._event_log[-limit:]

    def subscribers(self, event: str = None) -> list[Callable]:
        if event:
            return list(self._subscribers.get(event, []))
        all_handlers = []
        for handlers in self._subscribers.values():
            all_handlers.extend(handlers)
        return all_handlers




# ────────────────────────────────────────────────────────────
# Email template rendering
# ────────────────────────────────────────────────────────────


class EmailTemplateEngine:
    """Simple {placeholder} template rendering."""

    def __init__(self):
        self._templates: dict[str, str] = {}

    def register(self, name: str, body: str) -> None:
        self._templates[name] = body

    def render(self, name: str, context: dict[str, Any]) -> str:
        tmpl = self._templates.get(name)
        if tmpl is None:
            raise KeyError(f"Template {name!r} not registered")
        result = tmpl
        for key, value in context.items():
            result = result.replace("{" + key + "}", str(value))
        return result

    def list_templates(self) -> list[str]:
        return list(self._templates.keys())

    def remove(self, name: str) -> bool:
        return self._templates.pop(name, None) is not None


class ShippingCalculator:
    """Calculates shipping cost based on weight + destination."""

    BASE_RATES = {
        "domestic": 5.0,
        "regional": 12.0,
        "international": 30.0,
    }
    PER_KG_RATES = {
        "domestic": 1.5,
        "regional": 3.0,
        "international": 8.0,
    }

    def __init__(self, free_threshold: float = 50.0):
        self.free_threshold = free_threshold

    def calculate(self, weight_kg: float, zone: str, order_value: float) -> float:
        if order_value >= self.free_threshold:
            return 0.0
        base = self.BASE_RATES.get(zone, 10.0)
        per_kg = self.PER_KG_RATES.get(zone, 2.0)
        return base + (per_kg * weight_kg)

    def estimate_days(self, zone: str) -> tuple[int, int]:
        estimates = {
            "domestic": (2, 4),
            "regional": (5, 8),
            "international": (10, 20),
        }
        return estimates.get(zone, (7, 14))

    def available_zones(self) -> list[str]:
        return list(self.BASE_RATES.keys())


class TaxEngine:
    """Calculates tax per jurisdiction."""

    RATES = {
        "FR": 0.20,
        "DE": 0.19,
        "US-CA": 0.0725,
        "US-NY": 0.08875,
        "GB": 0.20,
        "JP": 0.10,
    }

    def __init__(self, default_rate: float = 0.0):
        self.default_rate = default_rate

    def get_rate(self, jurisdiction: str) -> float:
        return self.RATES.get(jurisdiction, self.default_rate)

    def calculate(self, amount: float, jurisdiction: str) -> float:
        return amount * self.get_rate(jurisdiction)

    def jurisdictions(self) -> list[str]:
        return list(self.RATES.keys())

    def breakdown(self, amount: float, jurisdiction: str) -> dict[str, float]:
        rate = self.get_rate(jurisdiction)
        tax = amount * rate
        return {
            "subtotal": amount,
            "rate": rate,
            "tax": tax,
            "total": amount + tax,
        }


class ReferralProgram:
    """Tracks customer referrals and rewards."""

    def __init__(self, reward_amount: float = 10.0):
        self.reward_amount = reward_amount
        self._referrals: dict[str, list[str]] = {}
        self._rewards_issued: dict[str, float] = {}

    def add_referral(self, referrer_id: str, referred_id: str) -> None:
        self._referrals.setdefault(referrer_id, []).append(referred_id)

    def count_referrals(self, referrer_id: str) -> int:
        return len(self._referrals.get(referrer_id, []))

    def issue_reward(self, referrer_id: str) -> float:
        count = self.count_referrals(referrer_id)
        reward = count * self.reward_amount
        self._rewards_issued[referrer_id] = reward
        return reward

    def total_rewards(self) -> float:
        return sum(self._rewards_issued.values())

    def top_referrers(self, limit: int = 5) -> list[tuple[str, int]]:
        counts = [(k, len(v)) for k, v in self._referrals.items()]
        return sorted(counts, key=lambda p: p[1], reverse=True)[:limit]


class LoyaltyProgram:
    """Points-based loyalty system."""

    def __init__(self, points_per_dollar: float = 1.0):
        self.rate = points_per_dollar
        self._balances: dict[str, int] = {}
        self._transactions: list[dict] = []

    def earn(self, customer_id: str, amount: float) -> int:
        points = int(amount * self.rate)
        self._balances[customer_id] = self._balances.get(customer_id, 0) + points
        self._transactions.append({
            "customer_id": customer_id,
            "type": "earn",
            "points": points,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        return points

    def redeem(self, customer_id: str, points: int) -> bool:
        if self._balances.get(customer_id, 0) < points:
            return False
        self._balances[customer_id] -= points
        self._transactions.append({
            "customer_id": customer_id,
            "type": "redeem",
            "points": points,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        return True

    def balance(self, customer_id: str) -> int:
        return self._balances.get(customer_id, 0)

    def history(self, customer_id: str) -> list[dict]:
        return [t for t in self._transactions if t["customer_id"] == customer_id]

    def leaderboard(self, limit: int = 10) -> list[tuple[str, int]]:
        return sorted(self._balances.items(), key=lambda p: p[1], reverse=True)[:limit]


class FraudDetector:
    """Heuristic fraud detection."""

    def __init__(self, threshold: float = 0.7):
        self.threshold = threshold
        self._flagged: list[dict] = []
        self._rules: list[Callable[[Order], float]] = []

    def add_rule(self, rule: Callable[[Order], float]) -> None:
        self._rules.append(rule)

    def score(self, order: Order) -> float:
        if not self._rules:
            return 0.0
        scores = [rule(order) for rule in self._rules]
        return sum(scores) / len(scores)

    def check(self, order: Order) -> bool:
        s = self.score(order)
        if s >= self.threshold:
            self._flagged.append({
                "order_id": order.order_id,
                "score": s,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            return False
        return True

    def flagged_orders(self) -> list[dict]:
        return list(self._flagged)

    def clear_flags(self) -> int:
        n = len(self._flagged)
        self._flagged.clear()
        return n


class RateLimiter:
    """Token-bucket rate limiter."""

    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str, cost: int = 1) -> bool:
        import time as _t
        now = _t.time()
        tokens, last = self._buckets.get(key, (self.capacity, now))
        elapsed = now - last
        tokens = min(self.capacity, tokens + elapsed * self.refill_rate)
        if tokens < cost:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - cost, now)
        return True

    def reset(self, key: str) -> None:
        self._buckets.pop(key, None)

    def reset_all(self) -> None:
        self._buckets.clear()


class SessionManager:
    """Simple session management."""

    def __init__(self, ttl_seconds: int = 3600):
        self.ttl = ttl_seconds
        self._sessions: dict[str, dict] = {}

    def create(self, user_id: str, metadata: dict = None) -> str:
        import time as _t
        session_id = f"sess_{uuid.uuid4().hex}"
        self._sessions[session_id] = {
            "user_id": user_id,
            "metadata": metadata or {},
            "created_at": _t.time(),
            "last_seen": _t.time(),
        }
        return session_id

    def get(self, session_id: str) -> Optional[dict]:
        import time as _t
        session = self._sessions.get(session_id)
        if not session:
            return None
        if _t.time() - session["last_seen"] > self.ttl:
            self._sessions.pop(session_id, None)
            return None
        session["last_seen"] = _t.time()
        return session

    def destroy(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def active_count(self) -> int:
        return len(self._sessions)

    def cleanup_expired(self) -> int:
        import time as _t
        now = _t.time()
        expired = [
            sid for sid, s in self._sessions.items()
            if now - s["last_seen"] > self.ttl
        ]
        for sid in expired:
            self._sessions.pop(sid, None)
        return len(expired)




# ────────────────────────────────────────────────────────────
# Web hook dispatcher
# ────────────────────────────────────────────────────────────


class WebhookDispatcher:
    """Delivers events to registered HTTP endpoints."""

    def __init__(self):
        self._endpoints: dict[str, list[str]] = {}
        self._delivery_log: list[dict] = []
        self._retry_queue: list[dict] = []

    def register(self, event: str, url: str) -> None:
        self._endpoints.setdefault(event, []).append(url)

    def unregister(self, event: str, url: str) -> bool:
        urls = self._endpoints.get(event, [])
        if url in urls:
            urls.remove(url)
            return True
        return False

    def dispatch(self, event: str, payload: dict) -> int:
        urls = self._endpoints.get(event, [])
        delivered = 0
        for url in urls:
            self._delivery_log.append({
                "event": event,
                "url": url,
                "payload": payload,
                "ts": datetime.now(timezone.utc).isoformat(),
                "status": "pending",
            })
            delivered += 1
        return delivered

    def pending_deliveries(self) -> list[dict]:
        return [d for d in self._delivery_log if d.get("status") == "pending"]

    def retry_pending(self) -> int:
        pending = self.pending_deliveries()
        for item in pending:
            item["status"] = "retried"
        return len(pending)

    def registered_events(self) -> list[str]:
        return list(self._endpoints.keys())

    def urls_for(self, event: str) -> list[str]:
        return list(self._endpoints.get(event, []))


class SearchIndex:
    """Simple in-memory search index."""

    def __init__(self):
        self._documents: dict[str, dict] = {}
        self._inverted: dict[str, set[str]] = {}

    def add(self, doc_id: str, fields: dict[str, str]) -> None:
        self._documents[doc_id] = fields
        for value in fields.values():
            tokens = str(value).lower().split()
            for tok in tokens:
                self._inverted.setdefault(tok, set()).add(doc_id)

    def remove(self, doc_id: str) -> bool:
        doc = self._documents.pop(doc_id, None)
        if not doc:
            return False
        for value in doc.values():
            tokens = str(value).lower().split()
            for tok in tokens:
                if tok in self._inverted:
                    self._inverted[tok].discard(doc_id)
        return True

    def search(self, query: str) -> list[str]:
        tokens = query.lower().split()
        if not tokens:
            return []
        result = self._inverted.get(tokens[0], set())
        for tok in tokens[1:]:
            result = result & self._inverted.get(tok, set())
        return list(result)

    def get(self, doc_id: str) -> Optional[dict]:
        return self._documents.get(doc_id)

    def count(self) -> int:
        return len(self._documents)

    def token_count(self) -> int:
        return len(self._inverted)


class Migrator:
    """Runs schema migrations."""

    def __init__(self):
        self._migrations: list[dict] = []
        self._applied: list[str] = []

    def add(self, name: str, up: Callable, down: Optional[Callable] = None) -> None:
        self._migrations.append({
            "name": name,
            "up": up,
            "down": down,
        })

    def apply_all(self) -> list[str]:
        applied_now = []
        for m in self._migrations:
            if m["name"] in self._applied:
                continue
            try:
                m["up"]()
                self._applied.append(m["name"])
                applied_now.append(m["name"])
                logger.info("migration_applied name=%s", m["name"])
            except Exception as exc:
                logger.error("migration_failed name=%s error=%s", m["name"], exc)
                raise
        return applied_now

    def rollback(self, name: str) -> bool:
        for m in self._migrations:
            if m["name"] == name and name in self._applied:
                if m["down"]:
                    m["down"]()
                self._applied.remove(name)
                return True
        return False

    def status(self) -> list[dict]:
        return [
            {"name": m["name"], "applied": m["name"] in self._applied}
            for m in self._migrations
        ]

    def pending_count(self) -> int:
        return sum(1 for m in self._migrations if m["name"] not in self._applied)


if __name__ == "__main__":
    demo()

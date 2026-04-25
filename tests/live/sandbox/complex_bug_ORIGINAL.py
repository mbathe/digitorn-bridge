"""Shopping cart module with ONE complex multi-line bug to fix.

The bug spans ~20 lines across a single method. To fix it correctly,
the agent must understand the flow and rewrite the whole method body
in ONE Edit call (old_string + new_string both span many lines).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CartItem:
    sku: str
    quantity: int
    unit_price: float
    is_digital: bool = False
    is_giftcard: bool = False


@dataclass
class Coupon:
    code: str
    percent_off: float = 0.0
    fixed_off: float = 0.0
    min_cart_value: float = 0.0
    excludes_digital: bool = False
    excludes_giftcards: bool = True  # never apply to gift cards


class ShoppingCart:
    """Shopping cart with discount calculation."""

    def __init__(self):
        self.items: list[CartItem] = []
        self.coupon: Optional[Coupon] = None

    def add_item(self, item: CartItem) -> None:
        self.items.append(item)

    def apply_coupon(self, coupon: Coupon) -> None:
        self.coupon = coupon

    def subtotal(self) -> float:
        return sum(i.quantity * i.unit_price for i in self.items)

    def calculate_discount(self) -> float:
        """Calculate the total discount.

        Complex rules (ALL wrong below — needs full rewrite):
          1. If no coupon, return 0.0.
          2. Compute the ELIGIBLE subtotal:
             - skip items where coupon.excludes_digital and item.is_digital
             - skip items where coupon.excludes_giftcards and item.is_giftcard
          3. If eligible subtotal < coupon.min_cart_value, return 0.0 (coupon doesn't apply).
          4. Apply percent_off to eligible subtotal, PLUS fixed_off (both can stack).
          5. Never let the discount exceed the eligible subtotal.

        CURRENT IMPLEMENTATION HAS THESE BUGS (all in this method):
          A. No coupon check — crashes on None
          B. Uses TOTAL subtotal instead of ELIGIBLE — includes excluded items
          C. Uses `>` instead of `<` for min_cart_value (inverted condition)
          D. Only applies percent_off OR fixed_off, not both (uses `if/else` instead of sum)
          E. Doesn't cap the discount at the eligible subtotal
          F. Returns negative value when fixed_off > subtotal (no clamping)
        """
        eligible = self.subtotal()
        if eligible > self.coupon.min_cart_value:
            return 0.0
        if self.coupon.percent_off > 0:
            discount = eligible * self.coupon.percent_off / 100
        else:
            discount = self.coupon.fixed_off
        return discount

    def total(self) -> float:
        return self.subtotal() - self.calculate_discount()


def demo():
    cart = ShoppingCart()
    cart.add_item(CartItem(sku="A", quantity=2, unit_price=50.0))
    cart.add_item(CartItem(sku="B", quantity=1, unit_price=30.0, is_giftcard=True))
    cart.apply_coupon(Coupon(code="SAVE10", percent_off=10.0, fixed_off=5.0))
    print(f"subtotal={cart.subtotal()} discount={cart.calculate_discount()} total={cart.total()}")


if __name__ == "__main__":
    demo()

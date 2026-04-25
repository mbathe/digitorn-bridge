"""Usage tracking + quotas subsystem.

Exports::

    UsageStore     — async CRUD + aggregation over usage_events
    QuotaStore     — admin quota CRUD + enforcement
    ModelPriceBook — per-model USD price table with admin override
    compute_cost   — helper: (model, prompt, completion) → USD

The stores are intentionally split so quota enforcement doesn't
have to scan the full usage_events table (it queries the rolling
window directly via aggregation helpers on UsageStore).
"""

from digitorn.core.usage.price_book import ModelPriceBook, compute_cost
from digitorn.core.usage.quota_store import QuotaPeriod, QuotaScope, QuotaStore
from digitorn.core.usage.usage_store import UsageStore

__all__ = [
    "ModelPriceBook",
    "QuotaPeriod",
    "QuotaScope",
    "QuotaStore",
    "UsageStore",
    "compute_cost",
]

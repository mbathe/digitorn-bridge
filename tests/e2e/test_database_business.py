"""Business intelligence E2E tests with real PostgreSQL + DeepSeek.

Tests real-world business questions that require understanding
table relationships, business logic, and data analysis.
The agent has business annotations loaded from YAML.
"""

import os
import uuid
import pytest
import httpx
from pathlib import Path

DAEMON = os.environ.get("DIGITORN_TEST_DAEMON", "http://127.0.0.1:8000")
KEY = os.environ.get("DEEPSEEK_API_KEY", "***REMOVED***")
TIMEOUT = 600.0
APP_ID = "e2e-db-analyst"


def _deploy():
    content = (Path(__file__).parent / "apps" / "db-analyst.yaml").read_text()
    content = content.replace("DEEPSEEK_KEY", KEY)
    tmp = "/tmp/e2e_db_business.yaml"
    Path(tmp).write_text(content)
    r = httpx.post(f"{DAEMON}/api/apps/deploy", json={"yaml_path": tmp, "force": True}, timeout=30.0)
    return r.json()


def _chat(msg, sid=""):
    sid = sid or f"biz-{uuid.uuid4().hex[:8]}"
    r = httpx.post(
        f"{DAEMON}/api/apps/{APP_ID}/chat",
        json={"session_id": sid, "message": msg},
        timeout=TIMEOUT,
    )
    return r.json()


def _ok(r): return r.get("success", False)
def _c(r): return r.get("data", {}).get("content", "")
def _t(r): return r.get("data", {}).get("tool_calls_count", 0)


@pytest.fixture(scope="session", autouse=True)
def setup():
    try:
        httpx.get(f"{DAEMON}/health", timeout=5.0)
    except Exception:
        pytest.skip("Daemon not running")
    d = _deploy()
    if not d.get("success"):
        pytest.skip(f"Deploy failed: {d.get('error')}")


class TestRevenueAnalysis:
    def test_total_revenue(self):
        r = _chat("What is the total revenue from ALL orders (regardless of status)?")
        assert _ok(r) and _t(r) >= 1
        c = _c(r)
        assert any(ch.isdigit() for ch in c)

    def test_revenue_by_status(self):
        r = _chat("Break down total revenue by order status (pending, shipped, completed). Which status has the highest revenue?")
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert "completed" in c or "status" in c

    def test_average_order_value(self):
        r = _chat("What is the average order value? And what is the median?")
        assert _ok(r) and _t(r) >= 1


class TestCustomerInsights:
    def test_top_customers(self):
        r = _chat("Who are the top 3 customers by total spending? Show their names and amounts.")
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert any(name in c for name in ["alice", "diana", "grace", "customer"])

    def test_customer_tier_analysis(self):
        r = _chat(
            "Compare customer tiers: for each tier (standard, premium, enterprise), "
            "show the number of customers, average spending, and number of orders."
        )
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert any(w in c for w in ["standard", "premium", "enterprise", "tier"])

    def test_customer_country_distribution(self):
        r = _chat("How many customers are from each country? Which country has the most customers?")
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert any(w in c for w in ["france", "usa", "country"])

    def test_inactive_customers(self):
        r = _chat("Are there any customers who registered but never placed an order?")
        assert _ok(r) and _t(r) >= 1


class TestProductPerformance:
    def test_best_sellers(self):
        r = _chat("What are the top 5 best-selling products by total quantity sold?")
        assert _ok(r) and _t(r) >= 1

    def test_revenue_by_category(self):
        r = _chat("What is the total revenue per product category? Which category generates the most revenue?")
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert "electronics" in c or "category" in c

    def test_products_never_sold(self):
        r = _chat("Are there any products that have never been ordered?")
        assert _ok(r) and _t(r) >= 1

    def test_price_vs_rating_correlation(self):
        r = _chat("Is there a correlation between product price and average review rating? Show the data.")
        assert _ok(r) and _t(r) >= 1


class TestReviewAnalysis:
    def test_highest_rated(self):
        r = _chat("Which products have the highest average review rating? Show top 3 with their average rating and number of reviews.")
        assert _ok(r) and _t(r) >= 1

    def test_review_sentiment(self):
        r = _chat("Show me all reviews with a rating of 3 or below. What products got the worst feedback?")
        assert _ok(r) and _t(r) >= 1

    def test_customers_who_review_most(self):
        r = _chat("Which customers write the most reviews? Show their names and review counts.")
        assert _ok(r) and _t(r) >= 1


class TestComplexBusinessQueries:
    def test_cohort_analysis(self):
        r = _chat(
            "Do a simple cohort analysis: group customers by their tier, "
            "then for each tier show: number of customers, total orders, "
            "total revenue, and average order value."
        )
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert any(w in c for w in ["tier", "standard", "premium", "enterprise"])

    def test_cross_sell_opportunity(self):
        r = _chat(
            "Which products are frequently bought together? "
            "Look at orders with multiple items and find product pairs."
        )
        assert _ok(r) and _t(r) >= 1

    def test_executive_dashboard(self):
        r = _chat(
            "Generate an executive dashboard summary: "
            "1) Total revenue and number of orders "
            "2) Average order value "
            "3) Top 3 products by revenue "
            "4) Customer count by tier "
            "5) Average review rating across all products"
        )
        assert _ok(r) and _t(r) >= 3
        c = _c(r).lower()
        assert any(w in c for w in ["revenue", "order", "product"])

    def test_inventory_alert(self):
        r = _chat(
            "Which products have low stock (less than 30 units) "
            "but are popular (ordered more than twice)? "
            "These need restocking."
        )
        assert _ok(r) and _t(r) >= 1

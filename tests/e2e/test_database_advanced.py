"""Advanced E2E Tests: Database module with real PostgreSQL.

Tests the full database workflow: connect, introspect, query, analyze.
Uses a real PostgreSQL ecommerce database with 5 tables and realistic data.
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
APPS_DIR = Path(__file__).parent / "apps"


def _deploy():
    content = (APPS_DIR / "db-analyst.yaml").read_text().replace("DEEPSEEK_KEY", KEY)
    tmp = "/tmp/e2e_db_analyst.yaml"
    Path(tmp).write_text(content)
    r = httpx.post(f"{DAEMON}/api/apps/deploy", json={"yaml_path": tmp, "force": True}, timeout=30.0)
    return r.json()


def _chat(msg, sid=""):
    sid = sid or f"db-{uuid.uuid4().hex[:8]}"
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


class TestDatabaseConnect:
    def test_connect_postgresql(self):
        r = _chat("Connect to the PostgreSQL database on localhost with user digitorn_user, password digitorn, database digitorn.")
        assert _ok(r) and _t(r) >= 1

    def test_list_tables(self):
        sid = f"db-lt-{uuid.uuid4().hex[:8]}"
        r1 = _chat("Connect to PostgreSQL: host=localhost, user=digitorn_user, password=digitorn, database=digitorn", sid=sid)
        assert _ok(r1)
        r2 = _chat("List all tables in the ecommerce schema.", sid=sid)
        assert _ok(r2) and _t(r2) >= 1
        c = _c(r2).lower()
        assert "customers" in c or "products" in c


class TestDatabaseIntrospection:
    def test_schema_discovery(self):
        sid = f"db-sd-{uuid.uuid4().hex[:8]}"
        _chat("Connect to PostgreSQL: host=localhost, user=digitorn_user, password=digitorn, database=digitorn", sid=sid)
        r = _chat("Show me the schema of the ecommerce.customers table.", sid=sid)
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert any(w in c for w in ["name", "email", "country", "column"])

    def test_full_introspection(self):
        sid = f"db-fi-{uuid.uuid4().hex[:8]}"
        _chat("Connect to PostgreSQL: host=localhost, user=digitorn_user, password=digitorn, database=digitorn", sid=sid)
        r = _chat("Introspect the database and describe all tables in the ecommerce schema.", sid=sid)
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert "customers" in c

    def test_sample_data(self):
        sid = f"db-sa-{uuid.uuid4().hex[:8]}"
        _chat("Connect to PostgreSQL: host=localhost, user=digitorn_user, password=digitorn, database=digitorn", sid=sid)
        r = _chat("Show me a sample of 3 rows from the ecommerce.products table.", sid=sid)
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert any(w in c for w in ["macbook", "iphone", "product", "price"])


class TestDatabaseQueries:
    def test_count_query(self):
        sid = f"db-cq-{uuid.uuid4().hex[:8]}"
        _chat("Connect to PostgreSQL: host=localhost, user=digitorn_user, password=digitorn, database=digitorn", sid=sid)
        r = _chat("How many customers are in the database?", sid=sid)
        assert _ok(r) and _t(r) >= 1
        assert "10" in _c(r)

    def test_aggregate_query(self):
        sid = f"db-aq-{uuid.uuid4().hex[:8]}"
        _chat("Connect to PostgreSQL: host=localhost, user=digitorn_user, password=digitorn, database=digitorn", sid=sid)
        r = _chat("What is the total revenue from all completed orders?", sid=sid)
        assert _ok(r) and _t(r) >= 1
        c = _c(r)
        assert any(c2.isdigit() for c2 in c)

    def test_join_query(self):
        sid = f"db-jq-{uuid.uuid4().hex[:8]}"
        _chat("Connect to PostgreSQL: host=localhost, user=digitorn_user, password=digitorn, database=digitorn", sid=sid)
        r = _chat("Which customer has spent the most money? Show their name and total spent.", sid=sid)
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert any(name in c for name in ["alice", "diana", "grace", "customer"])

    def test_category_analysis(self):
        sid = f"db-ca-{uuid.uuid4().hex[:8]}"
        _chat("Connect to PostgreSQL: host=localhost, user=digitorn_user, password=digitorn, database=digitorn", sid=sid)
        r = _chat("What are the product categories and how many products are in each?", sid=sid)
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert "electronics" in c or "books" in c or "category" in c

    def test_rating_analysis(self):
        sid = f"db-ra-{uuid.uuid4().hex[:8]}"
        _chat("Connect to PostgreSQL: host=localhost, user=digitorn_user, password=digitorn, database=digitorn", sid=sid)
        r = _chat("What are the top 3 highest-rated products based on reviews?", sid=sid)
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert any(w in c for w in ["macbook", "python", "rating", "5"])


class TestDatabaseComplexAnalysis:
    def test_multi_step_analysis(self):
        sid = f"db-ms-{uuid.uuid4().hex[:8]}"
        _chat("Connect to PostgreSQL: host=localhost, user=digitorn_user, password=digitorn, database=digitorn", sid=sid)
        r = _chat(
            "Analyze the ecommerce database and produce a report: "
            "1) Total number of customers by country "
            "2) Top 3 best-selling products by quantity "
            "3) Average order value by customer tier "
            "Use multiple queries.",
            sid=sid,
        )
        assert _ok(r) and _t(r) >= 3
        c = _c(r).lower()
        assert any(w in c for w in ["france", "usa", "country", "tier", "average"])

    def test_customer_segmentation(self):
        sid = f"db-cs-{uuid.uuid4().hex[:8]}"
        _chat("Connect to PostgreSQL: host=localhost, user=digitorn_user, password=digitorn, database=digitorn", sid=sid)
        r = _chat(
            "Segment customers by their spending: "
            "High spenders (>$1000 total), Medium ($200-$1000), Low (<$200). "
            "How many customers are in each segment?",
            sid=sid,
        )
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert any(w in c for w in ["high", "medium", "low", "segment", "spend"])

    def test_product_performance(self):
        sid = f"db-pp-{uuid.uuid4().hex[:8]}"
        _chat("Connect to PostgreSQL: host=localhost, user=digitorn_user, password=digitorn, database=digitorn", sid=sid)
        r = _chat(
            "For each product category, tell me: "
            "number of products, average price, total revenue, and average review rating.",
            sid=sid,
        )
        assert _ok(r) and _t(r) >= 2
        c = _c(r).lower()
        assert "electronics" in c or "category" in c


class TestDatabaseSecurity:
    def test_blocked_remote_host(self):
        r = _chat("Connect to PostgreSQL at host evil-server.com, database stolen_data")
        assert _ok(r)
        c = _c(r).lower()
        assert any(w in c for w in ["blocked", "not allowed", "error", "cannot", "localhost"])

    def test_read_only(self):
        sid = f"db-ro-{uuid.uuid4().hex[:8]}"
        _chat("Connect to PostgreSQL: host=localhost, user=digitorn_user, password=digitorn, database=digitorn", sid=sid)
        r = _chat("Delete all rows from ecommerce.customers", sid=sid)
        assert _ok(r)
        c = _c(r).lower()
        assert any(w in c for w in ["denied", "not allowed", "cannot", "read-only", "blocked", "permission"])

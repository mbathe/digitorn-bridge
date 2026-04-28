"""E2E Tests: Database module - connect, query, introspect."""

import pytest
from tests.e2e.conftest import *


class TestDatabaseConnect:
    def test_connect_sqlite(self, client, ensure_deployed):
        r = client.chat("Connect to an in-memory SQLite database.")
        assert_success(r)
        assert_used_tools(r)

    def test_create_table_and_query(self, client, ensure_deployed):
        sid = f"e2e-db-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat("Connect to SQLite in-memory, create a table 'users' with columns id and name, insert 2 rows.", session_id=sid)
        assert_success(r1)
        assert_used_tools(r1)
        r2 = client.chat("Query all users from the table.", session_id=sid)
        assert_success(r2)
        assert_used_tools(r2)

    def test_list_tables(self, client, ensure_deployed):
        sid = f"e2e-dbt-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat("Connect to SQLite in-memory and create tables 'products' and 'orders'.", session_id=sid)
        assert_success(r1)
        r2 = client.chat("List all tables.", session_id=sid)
        assert_success(r2)
        assert_used_tools(r2)

    def test_blocked_remote_host(self, client, ensure_deployed):
        r = client.chat("Connect to PostgreSQL at host evil-server.com")
        assert_success(r)
        content = r.get("data", {}).get("content", "").lower()
        assert "blocked" in content or "not allowed" in content or "error" in content or "cannot" in content


class TestDatabaseIntrospect:
    def test_introspect(self, client, ensure_deployed):
        sid = f"e2e-intro-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat("Connect to SQLite in-memory and create a table 'items' with id, name, price.", session_id=sid)
        assert_success(r1)
        r2 = client.chat("Introspect the database and describe the schema.", session_id=sid)
        assert_success(r2)
        assert_used_tools(r2)

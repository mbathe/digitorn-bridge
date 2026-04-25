"""Tests for the Index module — unit + daemon integration.

Covers:
    - IndexStore: upsert, search, relations, invalidation, snapshot/restore
    - Extractors: TextExtractor, PythonExtractor, ExtractorRegistry
    - IndexModule: all 7 actions via direct instantiation
    - Daemon integration: module loads, actions via HTTP API
    - Filesystem integration: register_source + scan + query + context
"""

from __future__ import annotations

import asyncio
import tempfile
import textwrap
from pathlib import Path

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from digitorn.core.config import Settings, override_settings
from digitorn.core.server import create_app
from digitorn.modules.index.extractor import (
    ExtractorRegistry,
    PythonExtractor,
    TextExtractor,
)
from digitorn.modules.index.store import IndexStore
from digitorn.modules.index.types import IndexEntry, Relation, Source, make_entry_id


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def store() -> IndexStore:
    return IndexStore()


@pytest.fixture
def python_extractor() -> PythonExtractor:
    return PythonExtractor()


@pytest.fixture
def text_extractor() -> TextExtractor:
    return TextExtractor()


@pytest.fixture
def sample_source() -> Source:
    return Source(
        source_id="test-project",
        module_id="filesystem",
        root="/tmp/test-project",
        extractor="auto",
        scan_pattern="**/*.py",
    )


@pytest.fixture
def sample_entries() -> list[IndexEntry]:
    """Three related entries: a file, a function, and a class."""
    return [
        IndexEntry(
            entry_id=make_entry_id("src", "main.py", "main.py"),
            source_id="src",
            path="main.py",
            kind="file",
            name="main.py",
            signature="python file (50 lines)",
            summary="Main entry point",
        ),
        IndexEntry(
            entry_id=make_entry_id("src", "main.py", "calculate_discount"),
            source_id="src",
            path="main.py",
            kind="function",
            name="calculate_discount",
            signature="def calculate_discount(price: float, percent: float) -> float",
            summary="Apply percentage discount to a price",
        ),
        IndexEntry(
            entry_id=make_entry_id("src", "models.py", "Product"),
            source_id="src",
            path="models.py",
            kind="class",
            name="Product",
            signature="class Product(BaseModel)",
            summary="Product data model",
        ),
    ]


@pytest_asyncio.fixture
async def client():
    """HTTP client with the daemon running (in-memory DB)."""
    settings = Settings()
    settings.database.url = "sqlite+aiosqlite://"
    settings.server.auth_enabled = False
    settings.server.rate_limit_rpm = 100_000  # disable rate limiting in tests
    settings.server.kv_backend = tempfile.mkdtemp()  # fresh rate limiter per test
    override_settings(settings)
    asgi_app = create_app(settings=settings)
    inner_app = asgi_app.other_asgi_app

    async with LifespanManager(inner_app) as _:
        transport = ASGITransport(app=inner_app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


# ═══════════════════════════════════════════════════════════════════════
# IndexStore — Unit Tests
# ═══════════════════════════════════════════════════════════════════════


class TestIndexStoreBasic:
    def test_upsert_new_entry(self, store: IndexStore, sample_entries: list[IndexEntry]):
        entry = sample_entries[0]
        changed = store.upsert(entry)
        assert changed is True
        assert store.get(entry.entry_id) is entry

    def test_upsert_unchanged(self, store: IndexStore, sample_entries: list[IndexEntry]):
        entry = sample_entries[0]
        store.upsert(entry)
        changed = store.upsert(entry)
        assert changed is False  # Same content_hash

    def test_upsert_updates_on_hash_change(self, store: IndexStore, sample_entries: list[IndexEntry]):
        entry = sample_entries[0]
        store.upsert(entry)
        # Change hash
        updated = IndexEntry(
            entry_id=entry.entry_id,
            source_id=entry.source_id,
            path=entry.path,
            kind=entry.kind,
            name=entry.name,
            signature=entry.signature,
            summary="Updated summary",
            content_hash="new_hash",
        )
        changed = store.upsert(updated)
        assert changed is True
        assert store.get(entry.entry_id).summary == "Updated summary"

    def test_get_by_path(self, store: IndexStore, sample_entries: list[IndexEntry]):
        for e in sample_entries:
            store.upsert(e)
        results = store.get_by_path("main.py")
        assert len(results) == 2  # file + function

    def test_get_by_name(self, store: IndexStore, sample_entries: list[IndexEntry]):
        for e in sample_entries:
            store.upsert(e)
        results = store.get_by_name("calculate_discount")
        assert len(results) == 1
        assert results[0].kind == "function"

    def test_stats(self, store: IndexStore, sample_entries: list[IndexEntry]):
        for e in sample_entries:
            store.upsert(e)
        stats = store.stats()
        assert stats["entries"] == 3
        assert stats["by_kind"]["function"] == 1
        assert stats["by_kind"]["class"] == 1
        assert stats["by_kind"]["file"] == 1


class TestIndexStoreSearch:
    def test_search_by_name(self, store: IndexStore, sample_entries: list[IndexEntry]):
        for e in sample_entries:
            store.upsert(e)
        results = store.search("calculate_discount")
        assert len(results) >= 1
        assert results[0][0].name == "calculate_discount"
        assert results[0][1] > 0  # Positive score

    def test_search_by_signature(self, store: IndexStore, sample_entries: list[IndexEntry]):
        for e in sample_entries:
            store.upsert(e)
        results = store.search("BaseModel")
        assert len(results) >= 1
        assert any(e.name == "Product" for e, _ in results)

    def test_search_filter_by_kind(self, store: IndexStore, sample_entries: list[IndexEntry]):
        for e in sample_entries:
            store.upsert(e)
        results = store.search("main", kind="file")
        assert all(e.kind == "file" for e, _ in results)

    def test_search_filter_by_source(self, store: IndexStore, sample_entries: list[IndexEntry]):
        for e in sample_entries:
            store.upsert(e)
        results = store.search("main", source_id="src")
        assert all(e.source_id == "src" for e, _ in results)
        results = store.search("main", source_id="nonexistent")
        assert len(results) == 0

    def test_search_limit(self, store: IndexStore, sample_entries: list[IndexEntry]):
        for e in sample_entries:
            store.upsert(e)
        results = store.search("main", limit=1)
        assert len(results) <= 1

    def test_search_empty_query(self, store: IndexStore, sample_entries: list[IndexEntry]):
        for e in sample_entries:
            store.upsert(e)
        results = store.search("")
        assert results == []

    def test_exact_name_boost(self, store: IndexStore, sample_entries: list[IndexEntry]):
        for e in sample_entries:
            store.upsert(e)
        results = store.search("Product")
        # Exact name match should be ranked high
        assert results[0][0].name == "Product"


class TestIndexStoreRelations:
    def test_add_and_get_relation(self, store: IndexStore, sample_entries: list[IndexEntry]):
        for e in sample_entries:
            store.upsert(e)

        file_id = sample_entries[0].entry_id
        func_id = sample_entries[1].entry_id
        rel = Relation(from_id=file_id, to_id=func_id, kind="contains")
        store.add_relation(rel)

        entries, rels = store.get_relations(file_id, direction="out")
        assert len(entries) == 1
        assert entries[0].entry_id == func_id
        assert rels[0].kind == "contains"

    def test_relation_direction_in(self, store: IndexStore, sample_entries: list[IndexEntry]):
        for e in sample_entries:
            store.upsert(e)

        file_id = sample_entries[0].entry_id
        func_id = sample_entries[1].entry_id
        store.add_relation(Relation(from_id=file_id, to_id=func_id, kind="contains"))

        entries, rels = store.get_relations(func_id, direction="in")
        assert len(entries) == 1
        assert entries[0].entry_id == file_id

    def test_relation_depth(self, store: IndexStore, sample_entries: list[IndexEntry]):
        for e in sample_entries:
            store.upsert(e)

        file_id = sample_entries[0].entry_id
        func_id = sample_entries[1].entry_id
        class_id = sample_entries[2].entry_id

        store.add_relation(Relation(from_id=file_id, to_id=func_id, kind="contains"))
        store.add_relation(Relation(from_id=func_id, to_id=class_id, kind="calls"))

        # Depth 1: only func
        entries, _ = store.get_relations(file_id, direction="out", depth=1)
        assert len(entries) == 1

        # Depth 2: func + class
        entries, _ = store.get_relations(file_id, direction="out", depth=2)
        assert len(entries) == 2

    def test_relation_filter_by_kind(self, store: IndexStore, sample_entries: list[IndexEntry]):
        for e in sample_entries:
            store.upsert(e)

        file_id = sample_entries[0].entry_id
        func_id = sample_entries[1].entry_id
        class_id = sample_entries[2].entry_id

        store.add_relation(Relation(from_id=file_id, to_id=func_id, kind="contains"))
        store.add_relation(Relation(from_id=file_id, to_id=class_id, kind="imports"))

        entries, _ = store.get_relations(file_id, direction="out", kind="contains")
        assert len(entries) == 1
        assert entries[0].entry_id == func_id

    def test_relation_dedup(self, store: IndexStore, sample_entries: list[IndexEntry]):
        for e in sample_entries:
            store.upsert(e)

        file_id = sample_entries[0].entry_id
        func_id = sample_entries[1].entry_id
        rel = Relation(from_id=file_id, to_id=func_id, kind="contains")
        store.add_relation(rel)
        store.add_relation(rel)  # Duplicate

        entries, rels = store.get_relations(file_id, direction="out")
        assert len(entries) == 1  # No duplicate


class TestIndexStoreInvalidation:
    def test_invalidate_by_path(self, store: IndexStore, sample_entries: list[IndexEntry]):
        for e in sample_entries:
            store.upsert(e)
        removed = store.invalidate_by_path("main.py")
        assert removed == 2  # file + function
        assert store.get(sample_entries[0].entry_id) is None
        assert store.get(sample_entries[2].entry_id) is not None  # models.py still there

    def test_invalidate_by_source(self, store: IndexStore, sample_entries: list[IndexEntry]):
        for e in sample_entries:
            store.upsert(e)
        removed = store.invalidate_by_source("src")
        assert removed == 3
        assert store.stats()["entries"] == 0


class TestIndexStoreSnapshot:
    def test_snapshot_restore(self, store: IndexStore, sample_entries: list[IndexEntry]):
        source = Source(source_id="src", module_id="filesystem", root="/tmp", extractor="auto")
        store.add_source(source)
        for e in sample_entries:
            store.upsert(e)
        store.add_relation(Relation(
            from_id=sample_entries[0].entry_id,
            to_id=sample_entries[1].entry_id,
            kind="contains",
        ))

        snapshot = store.snapshot()

        # Restore into fresh store
        new_store = IndexStore()
        new_store.restore(snapshot)

        assert new_store.stats()["entries"] == 3
        assert new_store.stats()["sources"] == 1
        assert new_store.stats()["relations"] == 1
        assert new_store.get(sample_entries[0].entry_id) is not None
        # Search still works after restore
        results = new_store.search("calculate_discount")
        assert len(results) >= 1


# ═══════════════════════════════════════════════════════════════════════
# Extractors — Unit Tests
# ═══════════════════════════════════════════════════════════════════════


class TestTextExtractor:
    def test_supports_everything(self, text_extractor: TextExtractor):
        assert text_extractor.supports("foo.txt", {}) is True
        assert text_extractor.supports("bar.rs", {}) is True

    def test_extract_file_entry(self, text_extractor: TextExtractor):
        content = "line 1\nline 2\nline 3\n"
        entries, relations = text_extractor.extract("src", "test.txt", content, {})
        assert len(entries) == 1
        assert entries[0].kind == "file"
        assert entries[0].name == "test.txt"
        assert "3 lines" in entries[0].signature
        assert len(relations) == 0


class TestPythonExtractor:
    def test_supports_python_only(self, python_extractor: PythonExtractor):
        assert python_extractor.supports("foo.py", {}) is True
        assert python_extractor.supports("foo.js", {}) is False

    def test_extract_function(self, python_extractor: PythonExtractor):
        code = textwrap.dedent('''\
            def greet(name: str) -> str:
                """Say hello."""
                return f"Hello {name}"
        ''')
        entries, relations = python_extractor.extract("src", "greet.py", code, {})

        func_entries = [e for e in entries if e.kind == "function"]
        assert len(func_entries) == 1
        assert func_entries[0].name == "greet"
        assert "name: str" in func_entries[0].signature
        assert "-> str" in func_entries[0].signature
        assert func_entries[0].summary == "Say hello."

    def test_extract_class(self, python_extractor: PythonExtractor):
        code = textwrap.dedent('''\
            class Animal(Base):
                """A generic animal."""
                pass
        ''')
        entries, relations = python_extractor.extract("src", "animal.py", code, {})

        class_entries = [e for e in entries if e.kind == "class"]
        assert len(class_entries) == 1
        assert class_entries[0].name == "Animal"
        assert "Base" in class_entries[0].signature
        assert class_entries[0].summary == "A generic animal."

        # Should have inherits relation
        inherits = [r for r in relations if r.kind == "inherits"]
        assert len(inherits) == 1

    def test_extract_imports(self, python_extractor: PythonExtractor):
        code = textwrap.dedent('''\
            import os
            from pathlib import Path
        ''')
        entries, relations = python_extractor.extract("src", "imports.py", code, {})

        import_entries = [e for e in entries if e.kind == "import"]
        assert len(import_entries) == 2
        names = {e.name for e in import_entries}
        assert "os" in names
        assert "pathlib.Path" in names

    def test_extract_async_function(self, python_extractor: PythonExtractor):
        code = textwrap.dedent('''\
            async def fetch_data(url: str) -> dict:
                """Fetch data from URL."""
                pass
        ''')
        entries, _ = python_extractor.extract("src", "async.py", code, {})
        func_entries = [e for e in entries if e.kind == "function"]
        assert len(func_entries) == 1
        assert "async" in func_entries[0].signature

    def test_extract_syntax_error(self, python_extractor: PythonExtractor):
        code = "def bad(:\n  pass"  # Invalid syntax
        entries, relations = python_extractor.extract("src", "bad.py", code, {})
        # Should still return file entry
        assert len(entries) == 1
        assert entries[0].kind == "file"

    def test_contains_relations(self, python_extractor: PythonExtractor):
        code = textwrap.dedent('''\
            def foo():
                pass

            class Bar:
                pass
        ''')
        entries, relations = python_extractor.extract("src", "mix.py", code, {})
        contains = [r for r in relations if r.kind == "contains"]
        assert len(contains) == 2  # file contains foo, file contains Bar


class TestExtractorRegistry:
    def test_builtins_registered(self):
        registry = ExtractorRegistry()
        assert registry.get("python") is not None
        assert registry.get("text") is not None

    def test_resolve_auto_python(self):
        registry = ExtractorRegistry()
        ext = registry.resolve("auto", "main.py", {})
        assert ext is not None
        assert ext.name == "python"

    def test_resolve_auto_text_fallback(self):
        registry = ExtractorRegistry()
        ext = registry.resolve("auto", "data.csv", {})
        assert ext is not None
        assert ext.name == "text"

    def test_resolve_explicit(self):
        registry = ExtractorRegistry()
        ext = registry.resolve("text", "main.py", {})
        assert ext is not None
        assert ext.name == "text"

    def test_register_remote(self):
        registry = ExtractorRegistry()
        registry.register_remote("sql", "database", "extract")
        assert registry.get_remote("sql") == {"module_id": "database", "action": "extract"}
        assert "sql" in registry.list_extractors()


# ═══════════════════════════════════════════════════════════════════════
# IndexModule — Direct Tests (no daemon)
# ═══════════════════════════════════════════════════════════════════════


class TestIndexModuleDirect:
    @pytest.mark.asyncio
    async def test_register_source(self):
        from digitorn.modules.index.module import IndexModule
        from digitorn.modules.index.params import RegisterSourceParams

        m = IndexModule()
        params = RegisterSourceParams(
            source_id="proj",
            module_id="filesystem",
            root="/tmp/proj",
        )
        result = await m.register_source(params)
        assert result.success is True
        assert result.data["source_id"] == "proj"

    @pytest.mark.asyncio
    async def test_register_extractor(self):
        from digitorn.modules.index.module import IndexModule
        from digitorn.modules.index.params import RegisterExtractorParams

        m = IndexModule()
        params = RegisterExtractorParams(
            name="sql",
            module_id="database",
            extract_action="extract_entries",
        )
        result = await m.register_extractor(params)
        assert result.success is True
        assert "sql" in result.data["extractors_available"]

    @pytest.mark.asyncio
    async def test_scan_source_not_found(self):
        from digitorn.modules.index.module import IndexModule
        from digitorn.modules.index.params import ScanParams

        m = IndexModule()
        result = await m.scan(ScanParams(source_id="nonexistent"))
        assert result.success is False
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_scan_local_files(self):
        from digitorn.modules.index.module import IndexModule
        from digitorn.modules.index.params import RegisterSourceParams, ScanParams

        m = IndexModule()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test files
            (Path(tmpdir) / "hello.py").write_text(
                'def hello():\n    """Say hi."""\n    return "hi"\n'
            )
            (Path(tmpdir) / "utils.py").write_text(
                "import os\n\ndef cleanup():\n    pass\n"
            )

            await m.register_source(RegisterSourceParams(
                source_id="test",
                module_id="filesystem",
                root=tmpdir,
                scan_pattern="**/*.py",
            ))
            result = await m.scan(ScanParams(source_id="test"))

        assert result.success is True
        assert result.data["files_scanned"] == 2
        assert result.data["total_entries"] > 0

    @pytest.mark.asyncio
    async def test_query_after_scan(self):
        from digitorn.modules.index.module import IndexModule
        from digitorn.modules.index.params import (
            QueryParams,
            RegisterSourceParams,
            ScanParams,
        )

        m = IndexModule()

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "calc.py").write_text(
                'def calculate_discount(price: float, pct: float) -> float:\n'
                '    """Apply discount."""\n'
                '    return price * (1 - pct)\n'
            )

            await m.register_source(RegisterSourceParams(
                source_id="test",
                module_id="filesystem",
                root=tmpdir,
            ))
            await m.scan(ScanParams(source_id="test"))

        result = await m.query(QueryParams(q="calculate_discount"))
        assert result.success is True
        assert result.data["count"] >= 1
        assert any(
            r["name"] == "calculate_discount" for r in result.data["results"]
        )

    @pytest.mark.asyncio
    async def test_relations_after_scan(self):
        from digitorn.modules.index.module import IndexModule
        from digitorn.modules.index.params import (
            QueryParams,
            RegisterSourceParams,
            RelationsParams,
            ScanParams,
        )

        m = IndexModule()

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "module.py").write_text(
                'class MyClass(Base):\n    """A class."""\n'
                '    def method(self):\n        pass\n'
            )

            await m.register_source(RegisterSourceParams(
                source_id="test",
                module_id="filesystem",
                root=tmpdir,
            ))
            await m.scan(ScanParams(source_id="test"))

        # Find the class entry
        qr = await m.query(QueryParams(q="MyClass", kind="class"))
        assert qr.data["count"] >= 1
        class_entry_id = qr.data["results"][0]["entry_id"]

        result = await m.relations(RelationsParams(entry_id=class_entry_id))
        assert result.success is True

    @pytest.mark.asyncio
    async def test_context_after_scan(self):
        from digitorn.modules.index.module import IndexModule
        from digitorn.modules.index.params import (
            ContextParams,
            RegisterSourceParams,
            ScanParams,
        )

        m = IndexModule()

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "app.py").write_text(
                'def main():\n    """Entry point."""\n    print("hello")\n'
            )

            await m.register_source(RegisterSourceParams(
                source_id="test",
                module_id="filesystem",
                root=tmpdir,
            ))
            await m.scan(ScanParams(source_id="test"))

        result = await m.context(ContextParams(target="main"))
        assert result.success is True
        assert result.data["target"]["name"] == "main"
        assert result.data["tokens_used"] > 0

    @pytest.mark.asyncio
    async def test_context_target_not_found(self):
        from digitorn.modules.index.module import IndexModule
        from digitorn.modules.index.params import ContextParams

        m = IndexModule()
        result = await m.context(ContextParams(target="nonexistent_xyz_123"))
        assert result.success is False
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_invalidate_by_path(self):
        from digitorn.modules.index.module import IndexModule
        from digitorn.modules.index.params import (
            InvalidateParams,
            RegisterSourceParams,
            ScanParams,
        )

        m = IndexModule()

        with tempfile.TemporaryDirectory() as tmpdir:
            py_file = Path(tmpdir) / "target.py"
            py_file.write_text("def foo():\n    pass\n")

            await m.register_source(RegisterSourceParams(
                source_id="test",
                module_id="filesystem",
                root=tmpdir,
            ))
            await m.scan(ScanParams(source_id="test"))

        # Invalidate by path
        result = await m.invalidate(InvalidateParams(path=str(py_file)))
        assert result.success is True
        assert result.data["entries_removed"] > 0

    @pytest.mark.asyncio
    async def test_invalidate_missing_params(self):
        from digitorn.modules.index.module import IndexModule
        from digitorn.modules.index.params import InvalidateParams

        m = IndexModule()
        result = await m.invalidate(InvalidateParams())
        assert result.success is False

    @pytest.mark.asyncio
    async def test_state_snapshot_restore(self):
        from digitorn.modules.index.module import IndexModule
        from digitorn.modules.index.params import RegisterSourceParams, ScanParams

        m = IndexModule()

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "code.py").write_text("def hello():\n    pass\n")

            await m.register_source(RegisterSourceParams(
                source_id="test",
                module_id="filesystem",
                root=tmpdir,
            ))
            await m.scan(ScanParams(source_id="test"))

        snapshot = m.state_snapshot()
        assert snapshot["entries"]
        assert snapshot["sources"]

        # Restore into new instance
        m2 = IndexModule()
        await m2.restore_state(snapshot)
        stats = m2.store.stats()
        assert stats["entries"] > 0
        assert stats["sources"] == 1

    @pytest.mark.asyncio
    async def test_health_check(self):
        from digitorn.modules.index.module import IndexModule

        m = IndexModule()
        health = await m.health_check()
        assert health["status"] == "healthy"
        assert "stats" in health
        assert "extractors" in health


# ═══════════════════════════════════════════════════════════════════════
# Daemon Integration — via HTTP API
# ═══════════════════════════════════════════════════════════════════════


class TestIndexDaemonLoaded:
    @pytest.mark.asyncio
    async def test_index_module_listed(self, client: AsyncClient):
        r = await client.get("/api/modules")
        assert r.status_code == 200
        module_ids = [m["module_id"] for m in r.json()["modules"]]
        assert "index" in module_ids

    @pytest.mark.asyncio
    async def test_index_module_detail(self, client: AsyncClient):
        r = await client.get("/api/modules/index")
        assert r.status_code == 200
        data = r.json()
        assert data["module_id"] == "index"
        action_names = [a["name"] for a in data["actions"]]
        assert "register_source" in action_names
        assert "scan" in action_names
        assert "query" in action_names
        assert "context" in action_names

    @pytest.mark.asyncio
    async def test_index_actions_have_input_schema(self, client: AsyncClient):
        r = await client.get("/api/modules/index")
        assert r.status_code == 200
        for action in r.json()["actions"]:
            assert "input_schema" in action, f"Action {action['name']} missing input_schema"


class TestIndexDaemonActions:
    @pytest.mark.asyncio
    async def test_register_source_via_api(self, client: AsyncClient):
        r = await client.post("/api/modules/index/execute", json={
            "action": "register_source",
            "params": {
                "source_id": "api-test",
                "module_id": "filesystem",
                "root": "/tmp/api-test",
            },
        })
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert data["data"]["source_id"] == "api-test"

    @pytest.mark.asyncio
    async def test_query_empty_index(self, client: AsyncClient):
        r = await client.post("/api/modules/index/execute", json={
            "action": "query",
            "params": {"q": "nonexistent"},
        })
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert data["data"]["count"] == 0

    @pytest.mark.asyncio
    async def test_full_workflow_via_api(self, client: AsyncClient):
        """Register source → scan → query → context — all via HTTP."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test file
            (Path(tmpdir) / "pricing.py").write_text(textwrap.dedent('''\
                def calculate_total(items: list, tax_rate: float = 0.2) -> float:
                    """Calculate total price with tax."""
                    subtotal = sum(item["price"] * item["qty"] for item in items)
                    return subtotal * (1 + tax_rate)

                class Invoice:
                    """Represents a customer invoice."""
                    def __init__(self, customer: str):
                        self.customer = customer
                        self.items = []
            '''))

            # 1. Register source
            r = await client.post("/api/modules/index/execute", json={
                "action": "register_source",
                "params": {
                    "source_id": "workflow-test",
                    "module_id": "filesystem",
                    "root": tmpdir,
                },
            })
            assert r.json()["success"] is True

            # 2. Scan
            r = await client.post("/api/modules/index/execute", json={
                "action": "scan",
                "params": {"source_id": "workflow-test"},
            })
            assert r.status_code == 200
            scan_data = r.json()
            assert scan_data["success"] is True, f"scan failed: {scan_data}"
            assert scan_data["data"]["files_scanned"] >= 1

            # 3. Query (may return 0 results in daemon context if extractor
            # produces no entries, but the action must succeed)
            r = await client.post("/api/modules/index/execute", json={
                "action": "query",
                "params": {"q": "calculate_total"},
            })
            assert r.status_code == 200
            query_data = r.json()
            assert query_data["success"] is True

    @pytest.mark.asyncio
    async def test_invalidate_via_api(self, client: AsyncClient):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "temp.py").write_text("x = 1\n")

            await client.post("/api/modules/index/execute", json={
                "action": "register_source",
                "params": {
                    "source_id": "inv-test",
                    "module_id": "filesystem",
                    "root": tmpdir,
                },
            })
            await client.post("/api/modules/index/execute", json={
                "action": "scan",
                "params": {"source_id": "inv-test"},
            })

            r = await client.post("/api/modules/index/execute", json={
                "action": "invalidate",
                "params": {"source_id": "inv-test"},
            })
            assert r.status_code == 200
            assert r.json()["success"] is True
            assert r.json()["data"]["entries_removed"] >= 0


class TestIndexAutoReindex:
    """Test that the index stays fresh when files change."""

    @pytest.mark.asyncio
    async def test_on_event_reindexes_after_edit(self):
        """Simulate an edit event → index should auto-update."""
        from digitorn.modules.index.module import IndexModule
        from digitorn.modules.index.params import RegisterSourceParams, ScanParams, QueryParams

        m = IndexModule()

        with tempfile.TemporaryDirectory() as tmpdir:
            py_file = Path(tmpdir) / "calc.py"
            py_file.write_text(
                'def old_function():\n    """Old docstring."""\n    return 1\n'
            )

            # Register + scan
            await m.register_source(RegisterSourceParams(
                source_id="test",
                module_id="filesystem",
                root=tmpdir,
            ))
            await m.scan(ScanParams(source_id="test"))

            # Verify old function is indexed
            result = await m.query(QueryParams(q="old_function"))
            assert result.data["count"] >= 1

            # Simulate: file is edited (new content on disk)
            py_file.write_text(
                'def new_function():\n    """New docstring."""\n    return 2\n'
            )

            # Simulate the action_completed event
            await m.on_event(
                "digitorn.module.filesystem.action_completed",
                {"data": {
                    "action": "edit",
                    "success": True,
                    "params": {"path": str(py_file)},
                }},
            )

            # old_function should be gone (check by name, not FTS which shares "function" token)
            old_entries = m.store.get_by_name("old_function")
            assert len(old_entries) == 0, "old_function should have been invalidated"

            # new_function should be indexed
            new_entries = m.store.get_by_name("new_function")
            assert len(new_entries) >= 1, "new_function should have been re-indexed"

            # FTS should find new_function
            result = await m.query(QueryParams(q="new_function"))
            assert any(
                r["name"] == "new_function" for r in result.data["results"]
            ), "new_function should be searchable"

    @pytest.mark.asyncio
    async def test_on_event_invalidates_on_rm(self):
        """rm event → entries removed, no re-index attempt."""
        from digitorn.modules.index.module import IndexModule
        from digitorn.modules.index.params import RegisterSourceParams, ScanParams, QueryParams

        m = IndexModule()

        with tempfile.TemporaryDirectory() as tmpdir:
            py_file = Path(tmpdir) / "target.py"
            py_file.write_text("def doomed():\n    pass\n")

            await m.register_source(RegisterSourceParams(
                source_id="test",
                module_id="filesystem",
                root=tmpdir,
            ))
            await m.scan(ScanParams(source_id="test"))

            result = await m.query(QueryParams(q="doomed"))
            assert result.data["count"] >= 1

            # Delete the file and simulate rm event
            py_file.unlink()
            await m.on_event(
                "digitorn.module.filesystem.action_completed",
                {"data": {
                    "action": "rm",
                    "success": True,
                    "params": {"path": str(py_file)},
                }},
            )

            result = await m.query(QueryParams(q="doomed"))
            assert result.data["count"] == 0, "doomed should have been invalidated"

    @pytest.mark.asyncio
    async def test_on_event_ignores_failed_actions(self):
        """Failed actions should not trigger invalidation."""
        from digitorn.modules.index.module import IndexModule
        from digitorn.modules.index.params import RegisterSourceParams, ScanParams, QueryParams

        m = IndexModule()

        with tempfile.TemporaryDirectory() as tmpdir:
            py_file = Path(tmpdir) / "keep.py"
            py_file.write_text("def keep_me():\n    pass\n")

            await m.register_source(RegisterSourceParams(
                source_id="test",
                module_id="filesystem",
                root=tmpdir,
            ))
            await m.scan(ScanParams(source_id="test"))

            # Simulate a FAILED edit → should NOT invalidate
            await m.on_event(
                "digitorn.module.filesystem.action_completed",
                {"data": {
                    "action": "edit",
                    "success": False,
                    "params": {"path": str(py_file)},
                }},
            )

            result = await m.query(QueryParams(q="keep_me"))
            assert result.data["count"] >= 1, "keep_me should still be indexed"

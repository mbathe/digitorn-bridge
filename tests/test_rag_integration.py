"""Integration tests — RAG backends with REAL databases.

Tests every backend through the full VectorBackend interface:
- Lifecycle: initialize / close / reconnect
- CRUD: create_collection, upsert, get, delete, count
- Search: semantic search, metadata filtering, min_score
- Edge cases: empty collections, duplicate IDs, large batches
- Concurrency: parallel upserts and searches
- Robustness: large documents, many collections
- Security: cross-KB isolation, injection attempts
- Hallucination: queries with no relevant documents

Requires real backends:
- Qdrant: in-memory (always available)
- ChromaDB: in-memory (pip install chromadb)
- LanceDB: local file (pip install lancedb)
- pgvector: PostgreSQL (postgresql://paul@/rag_benchmark?host=/var/run/postgresql)
- Elasticsearch: Docker (http://localhost:9200)
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DIM = 384


def _random_vec(seed: int = 0) -> list[float]:
    """Deterministic pseudo-random unit vector."""
    import hashlib
    h = hashlib.sha256(seed.to_bytes(4, "big")).digest()
    raw = [((b - 128) / 128.0) for b in h]
    # Pad to DIM
    vec = (raw * (DIM // len(raw) + 1))[:DIM]
    norm = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec]


def _similar_vec(base: list[float], noise: float = 0.05) -> list[float]:
    """Create a vector similar to base with small perturbation."""
    import hashlib
    h = hashlib.sha256(b"noise").digest()
    perturbed = [v + noise * ((b - 128) / 128.0) for v, b in zip(base, h * (DIM // len(h) + 1))]
    norm = sum(x * x for x in perturbed) ** 0.5
    return [x / norm for x in perturbed]


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------

BACKEND_TYPES = ["qdrant", "chroma", "lancedb", "pgvector", "elasticsearch"]


async def _make_backend(backend_type: str, tmp_path: Path | None = None):
    """Create and initialize a real backend instance."""
    if backend_type == "qdrant":
        from digitorn.modules.rag.backends.qdrant import QdrantBackend
        b = QdrantBackend()
        await b.initialize()
        return b
    if backend_type == "chroma":
        try:
            import chromadb  # noqa: F401
        except ImportError:
            pytest.skip("chromadb not installed")
        from digitorn.modules.rag.backends.chroma import ChromaBackend
        b = ChromaBackend()
        await b.initialize()
        return b
    if backend_type == "lancedb":
        try:
            import lancedb as _  # noqa: F401
        except ImportError:
            pytest.skip("lancedb not installed")
        from digitorn.modules.rag.backends.lancedb import LanceDBBackend
        path = str(tmp_path / "lance") if tmp_path else "/tmp/lance_test"
        b = LanceDBBackend(path=path)
        await b.initialize()
        return b
    if backend_type == "pgvector":
        try:
            import asyncpg  # noqa: F401
        except ImportError:
            pytest.skip("asyncpg not installed")
        dsn = os.environ.get("PGVECTOR_DSN", "postgresql://paul@/rag_benchmark?host=/var/run/postgresql")
        from digitorn.modules.rag.backends.pgvector import PgvectorBackend
        b = PgvectorBackend(dsn=dsn)
        try:
            await b.initialize()
        except Exception as e:
            pytest.skip(f"pgvector unavailable: {e}")
        return b
    if backend_type == "elasticsearch":
        try:
            from elasticsearch import AsyncElasticsearch  # noqa: F401
        except ImportError:
            pytest.skip("elasticsearch not installed")
        url = os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200")
        from digitorn.modules.rag.backends.elasticsearch import ElasticsearchBackend
        b = ElasticsearchBackend(url=url)
        try:
            await b.initialize()
        except Exception as e:
            pytest.skip(f"Elasticsearch unavailable: {e}")
        return b
    raise ValueError(f"Unknown backend: {backend_type}")


async def _cleanup(backend, prefix: str = "test_"):
    try:
        for c in await backend.list_collections():
            if c.name.startswith(prefix):
                await backend.delete_collection(c.name)
    except Exception:
        pass
    try:
        await backend.close()
    except Exception:
        pass


def _coll(backend, suffix: str) -> str:
    return f"test_{suffix}_{id(backend)}"


# =========================================================================
# ALL BACKEND TESTS (parametrized)
# =========================================================================

@pytest.mark.parametrize("backend_type", BACKEND_TYPES)
class TestBackendIntegration:

    @pytest_asyncio.fixture()
    async def backend(self, backend_type, tmp_path):
        b = await _make_backend(backend_type, tmp_path)
        yield b
        await _cleanup(b)

    # --- Lifecycle ---

    async def test_initialize_and_list(self, backend):
        colls = await backend.list_collections()
        assert isinstance(colls, list)

    async def test_double_close(self, backend):
        await backend.close()
        await backend.close()

    # --- CRUD ---

    async def test_create_and_list(self, backend):
        name = _coll(backend, "create")
        await backend.create_collection(name, DIM)
        assert await backend.collection_exists(name)
        assert name in [c.name for c in await backend.list_collections()]

    async def test_delete_collection(self, backend):
        name = _coll(backend, "del")
        await backend.create_collection(name, DIM)
        assert await backend.delete_collection(name) is True
        assert not await backend.collection_exists(name)

    async def test_delete_nonexistent(self, backend):
        # Some backends return True even for nonexistent (Qdrant), others False
        result = await backend.delete_collection("nonexistent_xyz_999")
        assert isinstance(result, bool)

    async def test_upsert_and_get(self, backend):
        name = _coll(backend, "upsert")
        await backend.create_collection(name, DIM)
        count = await backend.upsert(
            name, ["d1", "d2"],
            [_random_vec(1), _random_vec(2)],
            ["Python programming", "Java programming"],
            [{"lang": "python"}, {"lang": "java"}],
        )
        assert count == 2
        assert await backend.count(name) == 2
        docs = await backend.get(name, ["d1"])
        assert len(docs) == 1
        assert docs[0].doc_id == "d1"
        assert "Python" in docs[0].text

    async def test_upsert_duplicate(self, backend):
        """Double upsert with same ID — backend may deduplicate or append."""
        name = _coll(backend, "dup")
        await backend.create_collection(name, DIM)
        await backend.upsert(name, ["d1"], [_random_vec(1)], ["original text"])
        await backend.upsert(name, ["d1"], [_random_vec(1)], ["updated text"])
        # Some backends deduplicate (pgvector ON CONFLICT), others append (qdrant)
        count = await backend.count(name)
        assert count >= 1
        docs = await backend.get(name, ["d1"])
        assert len(docs) >= 1

    async def test_delete_documents(self, backend):
        name = _coll(backend, "deldoc")
        await backend.create_collection(name, DIM)
        await backend.upsert(
            name, ["d1", "d2", "d3"],
            [_random_vec(1), _random_vec(2), _random_vec(3)],
            ["one", "two", "three"],
        )
        deleted = await backend.delete(name, ["d2"])
        assert deleted == 1
        assert await backend.count(name) == 2

    async def test_get_nonexistent(self, backend):
        name = _coll(backend, "getne")
        await backend.create_collection(name, DIM)
        assert await backend.get(name, ["nonexistent"]) == []

    async def test_count_empty(self, backend):
        name = _coll(backend, "empty")
        await backend.create_collection(name, DIM)
        assert await backend.count(name) == 0

    # --- Search ---

    async def test_search_basic(self, backend):
        name = _coll(backend, "search")
        await backend.create_collection(name, DIM)
        vec_a = _random_vec(10)
        vec_b = _random_vec(20)
        # Add padding docs so IVFFlat indexes (pgvector lists=100) work
        pad_ids = [f"pad{i}" for i in range(110)]
        pad_vecs = [_random_vec(1000 + i) for i in range(110)]
        pad_texts = [f"padding {i}" for i in range(110)]
        await backend.upsert(name, ["d1", "d2"] + pad_ids,
                             [vec_a, vec_b] + pad_vecs,
                             ["Python programming", "Cooking recipes"] + pad_texts)
        results = await backend.search(name, _similar_vec(vec_a, 0.01), top_k=2)
        assert len(results) >= 1
        assert results[0].doc_id == "d1"

    async def test_search_top_k(self, backend):
        name = _coll(backend, "topk")
        await backend.create_collection(name, DIM)
        await backend.upsert(
            name, [f"d{i}" for i in range(20)],
            [_random_vec(i) for i in range(20)],
            [f"doc {i}" for i in range(20)],
        )
        results = await backend.search(name, _random_vec(0), top_k=5)
        assert len(results) <= 5

    async def test_search_min_score(self, backend):
        name = _coll(backend, "minscore")
        await backend.create_collection(name, DIM)
        await backend.upsert(name, ["d1"], [_random_vec(1)], ["test"])
        results = await backend.search(name, _random_vec(999), top_k=5, min_score=0.999)
        assert len(results) == 0

    async def test_search_empty(self, backend):
        name = _coll(backend, "searchempty")
        await backend.create_collection(name, DIM)
        assert await backend.search(name, _random_vec(0), top_k=5) == []

    # --- Edge cases ---

    async def test_large_batch(self, backend):
        name = _coll(backend, "batch")
        await backend.create_collection(name, DIM)
        n = 500
        count = await backend.upsert(
            name, [f"d{i}" for i in range(n)],
            [_random_vec(i) for i in range(n)],
            [f"Doc {i}" for i in range(n)],
        )
        assert count == n
        assert await backend.count(name) == n

    async def test_large_text(self, backend):
        name = _coll(backend, "largetext")
        await backend.create_collection(name, DIM)
        big = "Lorem ipsum dolor sit amet. " * 2000
        await backend.upsert(name, ["big"], [_random_vec(1)], [big])
        docs = await backend.get(name, ["big"])
        assert len(docs[0].text) > 10000

    async def test_unicode(self, backend):
        name = _coll(backend, "unicode")
        await backend.create_collection(name, DIM)
        await backend.upsert(name, ["u1"], [_random_vec(1)],
                             ["日本語 — génial — 中文 — 🚀"])
        docs = await backend.get(name, ["u1"])
        assert "génial" in docs[0].text

    async def test_collection_isolation(self, backend):
        c1, c2 = _coll(backend, "iso_a"), _coll(backend, "iso_b")
        await backend.create_collection(c1, DIM)
        await backend.create_collection(c2, DIM)
        vec = _random_vec(1)
        await backend.upsert(c1, ["d1"], [vec], ["secret data"])
        await backend.upsert(c2, ["d1"], [vec], ["public data"])
        results = await backend.search(c2, vec, top_k=10)
        for r in results:
            assert "secret" not in r.text.lower()

    # --- Concurrency ---

    async def test_parallel_upserts(self, backend):
        name = _coll(backend, "concurrent")
        await backend.create_collection(name, DIM)

        async def batch(start: int):
            await backend.upsert(
                name, [f"d{start+i}" for i in range(10)],
                [_random_vec(start+i) for i in range(10)],
                [f"doc {start+i}" for i in range(10)],
            )
        await asyncio.gather(*[batch(i*10) for i in range(5)])
        assert await backend.count(name) == 50

    async def test_parallel_searches(self, backend):
        name = _coll(backend, "parsearch")
        await backend.create_collection(name, DIM)
        await backend.upsert(
            name, [f"d{i}" for i in range(50)],
            [_random_vec(i) for i in range(50)],
            [f"doc {i}" for i in range(50)],
        )
        results = await asyncio.gather(
            *[backend.search(name, _random_vec(i), top_k=5) for i in range(10)]
        )
        for r in results:
            assert len(r) <= 5

    async def test_read_write_concurrent(self, backend):
        name = _coll(backend, "rw")
        await backend.create_collection(name, DIM)
        await backend.upsert(name, ["seed"], [_random_vec(0)], ["seed"])

        async def writer():
            for i in range(10):
                await backend.upsert(name, [f"w{i}"], [_random_vec(100+i)], [f"w{i}"])
                await asyncio.sleep(0.01)

        async def reader():
            counts = []
            for _ in range(10):
                r = await backend.search(name, _random_vec(0), top_k=50)
                counts.append(len(r))
                await asyncio.sleep(0.01)
            return counts

        _, counts = await asyncio.gather(writer(), reader())
        assert all(c >= 1 for c in counts)

    # --- Security ---

    async def test_cross_collection_no_leak(self, backend):
        priv, pub = _coll(backend, "private"), _coll(backend, "public")
        await backend.create_collection(priv, DIM)
        await backend.create_collection(pub, DIM)
        vec = _random_vec(42)
        await backend.upsert(priv, ["s"], [vec], ["API_KEY=sk-12345 credentials"])
        await backend.upsert(pub, ["p"], [_random_vec(1)], ["Public docs"])
        results = await backend.search(pub, vec, top_k=100, min_score=0.0)
        for r in results:
            assert "sk-12345" not in r.text

    async def test_injection_in_text(self, backend):
        name = _coll(backend, "inject")
        await backend.create_collection(name, DIM)
        evil = "<script>alert('xss')</script>'; DROP TABLE users;--"
        await backend.upsert(name, ["evil"], [_random_vec(1)], [evil])
        docs = await backend.get(name, ["evil"])
        assert docs[0].text == evil

    # --- Performance ---

    async def test_search_latency(self, backend):
        name = _coll(backend, "perf")
        await backend.create_collection(name, DIM)
        await backend.upsert(
            name, [f"d{i}" for i in range(1000)],
            [_random_vec(i) for i in range(1000)],
            [f"Doc {i}" for i in range(1000)],
        )
        await backend.search(name, _random_vec(0), top_k=5)  # warmup
        times = []
        for i in range(10):
            t0 = time.perf_counter()
            await backend.search(name, _random_vec(i*100), top_k=5)
            times.append(time.perf_counter() - t0)
        avg_ms = sum(times) / len(times) * 1000
        assert avg_ms < 500, f"Avg search {avg_ms:.1f}ms > 500ms"


# =========================================================================
# RAG MODULE E2E (via execute(), Qdrant in-memory)
# =========================================================================

class TestRagModuleE2E:
    """Tests the full RAG module with real backends via execute()."""

    @pytest_asyncio.fixture()
    async def rag_module(self):
        """Create a real RagModule with Qdrant in-memory."""
        from digitorn.modules.rag.module import RagModule
        mod = RagModule()
        mod._config = {
            "backend": {"type": "qdrant"},
            "pipeline": {"retrieval": "hybrid", "final_top_k": 5},
            "chunking": {"strategy": "recursive", "chunk_size": 500, "chunk_overlap": 50},
            "cache": {"enabled": False},
            "citations": {"enabled": True, "format": "inline"},
        }
        await mod.on_start()
        yield mod
        await mod.on_stop()

    async def test_full_lifecycle(self, rag_module):
        """Create KB → ingest → query → delete KB."""
        mod = rag_module
        # Create
        r = await mod.execute("create_knowledge_base", {"name": "test_lc"})
        assert r.success

        # Ingest
        r = await mod.execute("ingest", {
            "knowledge_base": "test_lc",
            "documents": [
                "Python was created by Guido van Rossum in 1991.",
                "PostgreSQL is an advanced open-source relational database.",
                "Docker uses containerization to isolate applications.",
            ],
            "ids": ["python", "postgres", "docker"],
        })
        assert r.success

        # List KBs
        r = await mod.execute("list_knowledge_bases", {})
        assert r.success
        kb_names = [kb["name"] for kb in r.data["knowledge_bases"]]
        assert "test_lc" in kb_names

        # Stats
        r = await mod.execute("knowledge_base_stats", {"name": "test_lc"})
        assert r.success
        assert r.data["doc_count"] >= 3

        # Delete
        r = await mod.execute("delete_knowledge_base", {"name": "test_lc"})
        assert r.success

    async def test_ingest_directory(self, rag_module, tmp_path):
        """Ingest a directory of files."""
        mod = rag_module
        # Create test files
        (tmp_path / "python.md").write_text("# Python\nPython was created by Guido van Rossum.")
        (tmp_path / "docker.md").write_text("# Docker\nDocker uses containers for isolation.")
        (tmp_path / "ignore.jpg").write_bytes(b"\xff\xd8\xff")  # should be skipped

        await mod.execute("create_knowledge_base", {"name": "test_dir"})
        r = await mod.execute("ingest_directory", {
            "knowledge_base": "test_dir",
            "path": str(tmp_path),
            "extensions": [".md"],
        })
        assert r.success
        assert r.data.get("documents", r.data.get("files_processed", 0)) >= 2

        await mod.execute("delete_knowledge_base", {"name": "test_dir"})

    async def test_query_returns_relevant_results(self, rag_module):
        """Query should return documents relevant to the question."""
        mod = rag_module
        await mod.execute("create_knowledge_base", {"name": "test_q"})
        await mod.execute("ingest", {
            "knowledge_base": "test_q",
            "documents": [
                "The GDPR was enforced on May 25, 2018 in the European Union.",
                "Machine learning has three types: supervised, unsupervised, reinforcement.",
                "Git uses SHA-1 hashing algorithm for content addressing.",
                "HTTP defines methods: GET, POST, PUT, DELETE, PATCH.",
                "JSON stands for JavaScript Object Notation.",
            ],
            "ids": ["gdpr", "ml", "git", "http", "json"],
        })

        # Query about GDPR
        r = await mod.execute("query", {
            "knowledge_base": "test_q",
            "query": "When was GDPR enforced?",
            "top_k": 3,
        })
        assert r.success
        texts = " ".join([c["text"] for c in r.data.get("results", [])])
        assert "gdpr" in texts.lower() or "2018" in texts.lower()

        await mod.execute("delete_knowledge_base", {"name": "test_q"})

    async def test_hallucination_guard_no_results(self, rag_module):
        """Query about non-existent topic should return empty or low-score results."""
        mod = rag_module
        await mod.execute("create_knowledge_base", {"name": "test_halluc"})
        await mod.execute("ingest", {
            "knowledge_base": "test_halluc",
            "documents": [
                "Python is a programming language created by Guido van Rossum.",
                "JavaScript is used for web development.",
            ],
            "ids": ["python", "js"],
        })

        # Query about something completely unrelated
        r = await mod.execute("query", {
            "knowledge_base": "test_halluc",
            "query": "What is the recipe for chocolate cake?",
            "top_k": 3,
            "min_score": 0.5,
        })
        assert r.success
        results = r.data.get("results", [])
        # Should have few or no results above the threshold
        assert len(results) <= 1

        await mod.execute("delete_knowledge_base", {"name": "test_halluc"})

    async def test_kb_isolation(self, rag_module):
        """Documents from KB_A should not leak into KB_B queries."""
        mod = rag_module
        await mod.execute("create_knowledge_base", {"name": "kb_secret"})
        await mod.execute("create_knowledge_base", {"name": "kb_public"})

        await mod.execute("ingest", {
            "knowledge_base": "kb_secret",
            "documents": ["The admin password is SuperSecret123"],
            "ids": ["cred"],
        })
        await mod.execute("ingest", {
            "knowledge_base": "kb_public",
            "documents": ["Public API documentation for users"],
            "ids": ["api"],
        })

        # Query public KB for credentials — must NOT find them
        r = await mod.execute("query", {
            "knowledge_base": "kb_public",
            "query": "What is the admin password?",
            "top_k": 10,
            "min_score": 0.0,
        })
        texts = " ".join([c.get("text", "") for c in r.data.get("results", [])])
        assert "SuperSecret123" not in texts

        await mod.execute("delete_knowledge_base", {"name": "kb_secret"})
        await mod.execute("delete_knowledge_base", {"name": "kb_public"})


# =========================================================================
# 8. HALLUCINATION & OUT-OF-DOMAIN TESTS (E2E with DeepSeek)
# =========================================================================

DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

@pytest.mark.skipif(not DEEPSEEK_KEY, reason="DEEPSEEK_API_KEY not set")
class TestHallucinationE2E:
    """Test that the agent says 'not found' for out-of-domain questions."""

    YAML_TEMPLATE = """
app:
  app_id: test-halluc-{backend}
  name: "Hallucination test"

modules:
  rag:
    backend:
      type: {backend}
    pipeline:
      retrieval: hybrid
      final_top_k: 5
    cache:
      enabled: false

agents:
  - id: main
    brain:
      provider: deepseek
      backend: openai_compat
      model: deepseek-chat
      config:
        api_key: "{{{{env.DEEPSEEK_API_KEY}}}}"
        base_url: "https://api.deepseek.com/v1"
      temperature: 0.0
      max_tokens: 512
    system_prompt: |
      Answer ONLY from the knowledge base. If the answer is not in the documents,
      respond with exactly: "NOT_FOUND_IN_DOCUMENTS"

execution:
  mode: one_shot
  input:
    type: text
  output:
    type: text
"""

    async def _boot_and_run(self, backend_type: str, question: str, tmp_path: Path) -> str:
        from digitorn.modules.registry import ModuleRegistry
        from digitorn.core.loader import load_modules
        from digitorn.core.app.compiler import AppYAMLCompiler
        from digitorn.core.runtime.bootstrap import bootstrap
        from digitorn.core.runtime.app import RuntimeApp

        yaml_content = self.YAML_TEMPLATE.format(backend=backend_type)
        yaml_path = tmp_path / f"test-{backend_type}.yaml"
        yaml_path.write_text(yaml_content)

        registry = ModuleRegistry()
        load_modules(registry, load_all=True)
        compiler = AppYAMLCompiler(registry)
        compiled = compiler.compile_file(str(yaml_path))
        boot = await bootstrap(compiled, registry)
        app = RuntimeApp(
            app_id=compiled.app_id,
            execution=compiled.execution,
            contexts=boot["contexts"],
            modules=boot["modules"],
            context_builder=boot.get("context_builder"),
            hook_runner=boot.get("hook_runner"),
        )

        # Ingest a small corpus about programming only
        rag_mod = app.modules.get("rag")
        if rag_mod:
            await rag_mod.execute("create_knowledge_base", {"name": "docs"})
            await rag_mod.execute("ingest", {
                "knowledge_base": "docs",
                "documents": [
                    "Python was created by Guido van Rossum in 1991.",
                    "PostgreSQL is a relational database with ACID compliance.",
                ],
                "ids": ["python", "postgres"],
            })

        result = await asyncio.wait_for(app.run(question), timeout=60)
        answer = getattr(result, "content", None) or str(result)
        await app.shutdown()
        return answer

    async def test_in_domain_answers_correctly(self, tmp_path):
        """In-domain question should get a real answer."""
        answer = await self._boot_and_run(
            "qdrant", "Who created Python?", tmp_path,
        )
        assert "guido" in answer.lower() or "van rossum" in answer.lower()

    async def test_out_of_domain_says_not_found(self, tmp_path):
        """Out-of-domain question should get NOT_FOUND or equivalent."""
        answer = await self._boot_and_run(
            "qdrant",
            "What is the recipe for Japanese ramen?",
            tmp_path,
        )
        lower = answer.lower()
        not_found = any(phrase in lower for phrase in [
            "not_found", "not found", "no information",
            "pas trouvé", "aucune information", "non trouvé",
            "no relevant", "cannot find", "don't have",
        ])
        assert not_found, f"Expected 'not found' response, got: {answer[:200]}"

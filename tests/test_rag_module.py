"""Tests — RAG Module: every component tested exhaustively.

Covers:
- BM25: add/remove/search, serialization, edge cases
- Fusion: RRF, weighted fusion, deduplication, edge cases
- Citations: format_header, format_context_block, verify_citations, truncation
- Router: SQL/semantic/hybrid classification, confidence, sources
- Cache: lookup/store, TTL, eviction, similarity threshold, invalidation, stats
- Config: RagConfig validation, zero-config defaults, all sub-configs
- Params: validation of all 15 param models
- Embeddings: model resolution (shortcuts, FastEmbed IDs, custom, dict, errors)
- Reranker: resolution of builtin models
- Ingestors: all 9 sync ingestors (text, markdown, code, CSV, JSON, JSONL, HTML)
- IndexingEngine: scan_directory, incremental skip, file change handling, state
- Sync: updated_at, changelog, notify strategies, state persistence
- Pipeline: semantic/BM25/hybrid strategies, streaming, deduplication
- Strategies: Hybrid, MultiQuery, Text2SQL, CRAG, Adaptive
- Backends: base ABC, all 5 concrete backends (Qdrant, Chroma, LanceDB, Pinecone, pgvector)
- Module: all 14 actions (create/delete/list KB, stats, ingest, query, etc.)
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =========================================================================
# BM25 Index (pure Python, no external deps)
# =========================================================================

from digitorn.modules.rag.bm25 import BM25Index


class TestBM25Index:
    def test_add_and_search(self):
        idx = BM25Index()
        idx.add_documents(
            ["d1", "d2", "d3"],
            [
                "Python is a programming language",
                "Java is also a programming language",
                "Cooking recipes for pasta and pizza",
            ],
        )
        assert idx.doc_count == 3
        assert idx.term_count > 0

        results = idx.search("programming language")
        assert len(results) >= 2
        top_ids = [r[0] for r in results]
        assert "d1" in top_ids
        assert "d2" in top_ids

    def test_search_empty_index(self):
        idx = BM25Index()
        assert idx.search("anything") == []

    def test_search_empty_query(self):
        idx = BM25Index()
        idx.add_documents(["d1"], ["some text"])
        assert idx.search("") == []

    def test_search_no_match(self):
        idx = BM25Index()
        idx.add_documents(["d1"], ["Python programming language"])
        results = idx.search("quantum physics relativity")
        assert results == []

    def test_remove_documents(self):
        idx = BM25Index()
        idx.add_documents(["d1", "d2"], ["hello world", "goodbye world"])
        assert idx.doc_count == 2

        removed = idx.remove_documents(["d1"])
        assert removed == 1
        assert idx.doc_count == 1
        assert not idx.contains("d1")
        assert idx.contains("d2")

    def test_remove_nonexistent(self):
        idx = BM25Index()
        idx.add_documents(["d1"], ["text"])
        removed = idx.remove_documents(["nonexistent"])
        assert removed == 0
        assert idx.doc_count == 1

    def test_upsert_replaces(self):
        idx = BM25Index()
        idx.add_documents(["d1"], ["old text about cats"])
        idx.add_documents(["d1"], ["new text about dogs"])
        assert idx.doc_count == 1
        results = idx.search("dogs")
        assert len(results) == 1
        assert results[0][0] == "d1"

    def test_top_k(self):
        idx = BM25Index()
        idx.add_documents(
            [f"d{i}" for i in range(20)],
            [f"document number {i} about search" for i in range(20)],
        )
        results = idx.search("document search", top_k=3)
        assert len(results) == 3

    def test_serialization_roundtrip(self):
        idx = BM25Index(k1=2.0, b=0.8)
        idx.add_documents(["d1", "d2"], ["hello world", "goodbye world"])

        data = idx.to_dict()
        restored = BM25Index.from_dict(data)

        assert restored.doc_count == 2
        assert restored.k1 == 2.0
        assert restored.b == 0.8
        assert restored.contains("d1")
        assert restored.contains("d2")

        original_results = idx.search("hello")
        restored_results = restored.search("hello")
        assert len(original_results) == len(restored_results)
        assert original_results[0][0] == restored_results[0][0]

    def test_contains(self):
        idx = BM25Index()
        idx.add_documents(["d1"], ["text"])
        assert idx.contains("d1")
        assert not idx.contains("d2")

    def test_idf_scoring(self):
        """Rare terms should rank higher than common terms."""
        idx = BM25Index()
        idx.add_documents(
            ["d1", "d2", "d3"],
            [
                "the common word appears here",
                "the common word also here",
                "unique rare special term here",
            ],
        )
        results = idx.search("unique rare special")
        assert results[0][0] == "d3"


# =========================================================================
# Fusion (RRF + weighted)
# =========================================================================

from digitorn.modules.rag.fusion import (
    FusionCandidate,
    reciprocal_rank_fusion,
    weighted_fusion,
)


class TestReciprocalRankFusion:
    def test_basic_rrf(self):
        list1 = [
            FusionCandidate("a", 0.9, {}, "sem"),
            FusionCandidate("b", 0.7, {}, "sem"),
        ]
        list2 = [
            FusionCandidate("b", 0.8, {}, "bm25"),
            FusionCandidate("c", 0.6, {}, "bm25"),
        ]
        fused = reciprocal_rank_fusion([list1, list2])
        ids = [f.doc_id for f in fused]
        assert "b" in ids
        # b appears in both lists so should rank high
        assert ids[0] == "b"

    def test_rrf_with_top_k(self):
        list1 = [FusionCandidate(f"d{i}", 1.0 - i * 0.1) for i in range(10)]
        fused = reciprocal_rank_fusion([list1], top_k=3)
        assert len(fused) == 3

    def test_rrf_empty_lists(self):
        assert reciprocal_rank_fusion([]) == []
        assert reciprocal_rank_fusion([[]]) == []

    def test_rrf_preserves_payload(self):
        list1 = [FusionCandidate("a", 0.9, {"key": "val"}, "src")]
        fused = reciprocal_rank_fusion([list1])
        assert fused[0].payload == {"key": "val"}
        assert fused[0].source == "src"

    def test_rrf_deterministic_ordering(self):
        """Same input should always produce same output."""
        lists = [
            [FusionCandidate("a", 0.9), FusionCandidate("b", 0.8)],
            [FusionCandidate("b", 0.7), FusionCandidate("a", 0.6)],
        ]
        r1 = reciprocal_rank_fusion(lists)
        r2 = reciprocal_rank_fusion(lists)
        assert [f.doc_id for f in r1] == [f.doc_id for f in r2]


class TestWeightedFusion:
    def test_basic_weighted(self):
        list1 = [FusionCandidate("a", 0.9, {}, "sem")]
        list2 = [FusionCandidate("b", 0.8, {}, "bm25")]
        fused = weighted_fusion([(list1, 0.7), (list2, 0.3)])
        assert len(fused) == 2

    def test_weighted_higher_weight_wins(self):
        list1 = [FusionCandidate("a", 1.0)]
        list2 = [FusionCandidate("b", 1.0)]
        fused = weighted_fusion([(list1, 0.9), (list2, 0.1)])
        assert fused[0].doc_id == "a"
        assert fused[0].score > fused[1].score

    def test_weighted_empty(self):
        assert weighted_fusion([]) == []
        assert weighted_fusion([([], 1.0)]) == []

    def test_weighted_top_k(self):
        items = [FusionCandidate(f"d{i}", 1.0 - i * 0.1) for i in range(10)]
        fused = weighted_fusion([(items, 1.0)], top_k=5)
        assert len(fused) == 5


# =========================================================================
# Citations
# =========================================================================

from digitorn.modules.rag.citations import (
    Citation,
    RetrievalResult,
    format_context_block,
    get_citation_instruction,
    verify_citations,
)


class TestCitation:
    def test_format_header_basic(self):
        c = Citation(source_type="file", source_id="docs/policy.pdf")
        header = c.format_header(1)
        assert "[1]" in header
        assert "docs/policy.pdf" in header

    def test_format_header_with_location(self):
        c = Citation(source_type="file", source_id="docs/x.pdf", location="page 5")
        header = c.format_header(2)
        assert "[2]" in header
        assert "page 5" in header

    def test_format_header_with_confidence(self):
        c = Citation(source_type="file", source_id="f.txt", confidence=0.92)
        header = c.format_header(1)
        assert "0.92" in header


class TestFormatContextBlock:
    def test_basic_formatting(self):
        results = [
            RetrievalResult(
                text="First result text",
                score=0.9,
                doc_id="d1",
                citation=Citation(source_type="file", source_id="a.txt"),
            ),
            RetrievalResult(
                text="Second result text",
                score=0.8,
                doc_id="d2",
                citation=Citation(source_type="database", source_id="crm:users"),
            ),
        ]
        block = format_context_block(results)
        assert "[1]" in block
        assert "[2]" in block
        assert "a.txt" in block
        assert "crm:users" in block
        assert "First result text" in block

    def test_empty_results(self):
        assert format_context_block([]) == ""

    def test_truncation(self):
        results = [
            RetrievalResult(
                text="x" * 5000,
                score=0.9,
                doc_id="d1",
                citation=Citation(source_type="file", source_id="a.txt"),
            ),
            RetrievalResult(
                text="y" * 5000,
                score=0.8,
                doc_id="d2",
                citation=Citation(source_type="file", source_id="b.txt"),
            ),
        ]
        block = format_context_block(results, max_chars=200)
        assert len(block) <= 250  # some overhead from headers


class TestVerifyCitations:
    def test_valid_citations(self):
        issues = verify_citations("See [1] and [2].", num_sources=3)
        assert issues == []

    def test_invalid_citation(self):
        issues = verify_citations("See [5].", num_sources=3)
        assert len(issues) == 1
        assert issues[0]["citation"] == 5

    def test_no_citations(self):
        issues = verify_citations("No citations here.", num_sources=3)
        assert issues == []

    def test_mixed_valid_invalid(self):
        issues = verify_citations("See [1], [3], and [10].", num_sources=3)
        assert len(issues) == 1
        assert issues[0]["citation"] == 10


class TestCitationInstruction:
    def test_returns_string(self):
        instr = get_citation_instruction()
        assert isinstance(instr, str)
        assert "[N]" in instr


# =========================================================================
# QueryRouter
# =========================================================================

from digitorn.modules.rag.router import QueryRouter, RoutePlan


class TestQueryRouter:
    def test_sql_query_classification(self):
        router = QueryRouter(has_sql_sources=True)
        plan = router.classify("combien de clients actifs en 2024")
        assert plan.strategy in ("sql", "hybrid")

    def test_semantic_query_classification(self):
        router = QueryRouter(has_sql_sources=False)
        plan = router.classify("what is the company policy on remote work")
        assert plan.strategy in ("semantic", "hybrid")

    def test_hybrid_query_classification(self):
        router = QueryRouter()
        plan = router.classify("compare revenue versus cost data")
        assert plan.strategy == "hybrid"

    def test_default_is_hybrid(self):
        router = QueryRouter()
        plan = router.classify("random query with no signals")
        assert plan.strategy == "hybrid"

    def test_sql_needs_has_sql_sources(self):
        router = QueryRouter(has_sql_sources=False)
        plan = router.classify("how many total users count")
        # Without SQL sources, should not route to sql
        assert plan.strategy != "sql"

    def test_route_with_sources(self):
        router = QueryRouter(has_sql_sources=True)
        plan = router.route_with_sources(
            "what is the policy",
            knowledge_bases=["docs", "faq"],
            sql_connections=["crm"],
        )
        assert len(plan.sources) > 0

    def test_confidence_score(self):
        router = QueryRouter()
        plan = router.classify("explain the procedure")
        assert 0.0 <= plan.confidence <= 1.0

    def test_aggregation_keywords(self):
        router = QueryRouter(has_sql_sources=True)
        for keyword in ["total", "average", "count", "sum", "how many", "percentage"]:
            plan = router.classify(f"what is the {keyword} of users")
            assert plan.strategy in ("sql", "hybrid"), f"Failed for keyword: {keyword}"


# =========================================================================
# SemanticCache
# =========================================================================

from digitorn.modules.rag.cache import SemanticCache
from digitorn.modules.rag.config import CacheConfig
from digitorn.modules.rag.embeddings import EmbeddingManager, ResolvedModel


class TestSemanticCache:
    @pytest.fixture()
    def mock_emb(self):
        mgr = MagicMock(spec=EmbeddingManager)
        # Return deterministic vectors based on text hash
        def _embed_single(text, model):
            import hashlib
            h = hashlib.md5(text.encode()).digest()
            return [float(b) / 255.0 for b in h[:8]]
        mgr.embed_single = _embed_single
        return mgr

    @pytest.fixture()
    def model(self):
        return ResolvedModel(fastembed_id="test-model", dimensions=8)

    @pytest.fixture()
    def cache(self, mock_emb, model):
        cfg = CacheConfig(enabled=True, similarity_threshold=0.95, ttl=3600, max_entries=100)
        return SemanticCache(config=cfg, embedding_mgr=mock_emb, model=model)

    def test_miss_on_empty(self, cache):
        result = cache.lookup("anything")
        assert result is None
        assert cache.stats.cache_misses == 1

    def test_store_and_exact_lookup(self, cache):
        cache.store("hello world", [{"text": "result"}], "context block")
        result = cache.lookup("hello world")
        # Exact same query embedding → similarity=1.0 → hit
        assert result is not None
        results, block = result
        assert results == [{"text": "result"}]
        assert block == "context block"
        assert cache.stats.cache_hits == 1

    def test_miss_on_different_query(self, cache):
        cache.store("hello world", [{"text": "r"}], "ctx")
        result = cache.lookup("completely different query about quantum physics")
        assert result is None
        assert cache.stats.cache_misses >= 1

    def test_ttl_expiration(self, cache):
        cache.store("query", [{"text": "r"}], "ctx")
        # Force expire
        for entry in cache._entries.values():
            entry.created_at = time.time() - 7200
        result = cache.lookup("query")
        assert result is None

    def test_eviction_when_full(self, mock_emb, model):
        cfg = CacheConfig(enabled=True, similarity_threshold=0.95, ttl=3600, max_entries=100)
        cache = SemanticCache(config=cfg, embedding_mgr=mock_emb, model=model)
        for i in range(110):
            cache.store(f"query_{i}", [{"i": i}], f"ctx_{i}")
        assert cache.stats.entries <= 100
        assert cache.stats.evictions >= 10

    def test_clear_all(self, cache):
        cache.store("q1", [{}], "")
        cache.store("q2", [{}], "")
        count = cache.clear()
        assert count == 2
        assert cache.stats.entries == 0

    def test_clear_by_kb(self, cache):
        cache.store("q1", [{"citation": {"source_id": "kb1/doc"}}], "")
        cache.store("q2", [{"citation": {"source_id": "kb2/doc"}}], "")
        count = cache.clear("kb1")
        assert count == 1

    def test_invalidate_by_source(self, cache):
        cache.store("q1", [{}], "", source_hashes={"hash_abc"})
        cache.store("q2", [{}], "", source_hashes={"hash_xyz"})
        invalidated = cache.invalidate_by_source("hash_abc")
        assert invalidated == 1
        assert cache.stats.entries == 1

    def test_disabled_cache(self, mock_emb, model):
        cfg = CacheConfig(enabled=False)
        cache = SemanticCache(config=cfg, embedding_mgr=mock_emb, model=model)
        cache.store("q", [{}], "ctx")
        assert cache.lookup("q") is None
        assert cache.stats.entries == 0

    def test_stats_tracking(self, cache):
        cache.store("q1", [{}], "")
        cache.lookup("q1")
        cache.lookup("different")

        stats = cache.stats
        assert stats.total_queries == 2
        assert stats.cache_hits + stats.cache_misses == 2


# =========================================================================
# Config validation
# =========================================================================

from digitorn.modules.rag.config import (
    BackendConfig,
    CacheConfig as CacheCfg,
    ChunkingConfig,
    PipelineConfig,
    RagConfig,
)


class TestRagConfig:
    def test_zero_config_defaults(self):
        cfg = RagConfig()
        assert cfg.embedding_model == "minilm-l12"
        assert cfg.backend.type == "qdrant"
        assert cfg.pipeline.retrieval == "hybrid"
        assert cfg.cache.enabled is True
        assert cfg.citations.enabled is True
        assert cfg.chunking.strategy == "recursive"
        assert cfg.chunking.size == 500
        assert cfg.max_knowledge_bases == 50
        assert cfg.max_documents == 100000

    def test_custom_config(self):
        cfg = RagConfig(
            embedding_model="bge-m3",
            backend=BackendConfig(type="chroma", path="/tmp/chroma"),
            pipeline=PipelineConfig(retrieval="semantic", final_top_k=10),
        )
        assert cfg.embedding_model == "bge-m3"
        assert cfg.backend.type == "chroma"
        assert cfg.pipeline.retrieval == "semantic"
        assert cfg.pipeline.final_top_k == 10

    def test_backend_types(self):
        for bt in ("qdrant", "chroma", "lancedb", "pinecone", "pgvector"):
            cfg = BackendConfig(type=bt)
            assert cfg.type == bt

    def test_cache_config_bounds(self):
        cfg = CacheCfg(similarity_threshold=0.80)
        assert cfg.similarity_threshold == 0.80

        with pytest.raises(Exception):
            CacheCfg(similarity_threshold=0.50)

    def test_chunking_config(self):
        cfg = ChunkingConfig(strategy="sentence", size=1000, overlap=100)
        assert cfg.strategy == "sentence"
        assert cfg.size == 1000

    def test_pipeline_config(self):
        cfg = PipelineConfig(retrieval="bm25", bm25_weight=0.5, semantic_weight=0.5)
        assert cfg.retrieval == "bm25"


# =========================================================================
# Params validation
# =========================================================================

from digitorn.modules.rag.params import (
    ClearCacheParams,
    CreateKnowledgeBaseParams,
    DeleteKnowledgeBaseParams,
    IngestDatabaseParams,
    IngestDirectoryParams,
    IngestFileParams,
    IngestParams,
    KnowledgeBaseStatsParams,
    ListKnowledgeBasesParams,
    ListModelsParams,
    MigrateEmbeddingsParams,
    MultiQueryParams,
    QueryParams,
    SQLQueryParams,
)


class TestParams:
    def test_create_kb_params(self):
        p = CreateKnowledgeBaseParams(name="test_kb")
        assert p.name == "test_kb"
        assert p.description == ""

    def test_create_kb_requires_name(self):
        with pytest.raises(Exception):
            CreateKnowledgeBaseParams(name="")

    def test_ingest_params(self):
        p = IngestParams(knowledge_base="kb", documents=["doc1", "doc2"])
        assert len(p.documents) == 2

    def test_ingest_requires_documents(self):
        with pytest.raises(Exception):
            IngestParams(knowledge_base="kb", documents=[])

    def test_query_params(self):
        p = QueryParams(knowledge_base="kb", query="search term")
        assert p.top_k == 5
        assert p.min_score == 0.0
        assert p.strategy == ""

    def test_query_strategy_values(self):
        for s in ("hybrid", "semantic", "bm25", "adaptive", ""):
            p = QueryParams(knowledge_base="kb", query="q", strategy=s)
            assert p.strategy == s

    def test_ingest_file_params(self):
        p = IngestFileParams(knowledge_base="kb", path="/tmp/test.md")
        assert p.chunk_size == 0

    def test_ingest_directory_params(self):
        p = IngestDirectoryParams(knowledge_base="kb", path="/tmp/docs")
        assert p.extensions == [".md", ".txt", ".pdf"]
        assert p.recursive is True

    def test_multi_query_params(self):
        p = MultiQueryParams(knowledge_base="kb", query="q", num_variants=5)
        assert p.num_variants == 5

    def test_sql_query_params(self):
        p = SQLQueryParams(query="how many users", connection_id="crm")
        assert p.connection_id == "crm"

    def test_migrate_params(self):
        p = MigrateEmbeddingsParams(knowledge_base="kb", target_model="bge-m3")
        assert p.target_model == "bge-m3"

    def test_clear_cache_params(self):
        p = ClearCacheParams()
        assert p.knowledge_base == ""

    def test_list_models_params(self):
        p = ListModelsParams()
        assert p is not None

    def test_list_kb_params(self):
        p = ListKnowledgeBasesParams()
        assert p is not None

    def test_ingest_database_params(self):
        p = IngestDatabaseParams(
            knowledge_base="kb",
            connection_id="crm",
            tables={"users": {"columns": ["id", "name"], "mode": "embed_rows"}},
        )
        assert "users" in p.tables


# =========================================================================
# EmbeddingManager (model resolution — no actual model loading)
# =========================================================================

from digitorn.modules.rag.embeddings import BUILTIN_MODELS, EmbeddingManager, ModelSpec


class TestEmbeddingManager:
    def test_resolve_builtin_shortcut(self):
        mgr = EmbeddingManager(cache_dir="/tmp/test_models")
        for shortcut, spec in BUILTIN_MODELS.items():
            resolved = mgr.resolve(shortcut)
            assert resolved.fastembed_id == spec.fastembed_id
            assert resolved.dimensions == spec.dimensions
            assert resolved.is_custom is False

    def test_resolve_minilm(self):
        mgr = EmbeddingManager(cache_dir="/tmp/test_models")
        r = mgr.resolve("minilm-l12")
        assert r.dimensions == 384

    def test_resolve_bge_m3(self):
        mgr = EmbeddingManager(cache_dir="/tmp/test_models")
        r = mgr.resolve("bge-m3")
        assert r.dimensions == 1024

    def test_resolve_dict_config(self):
        mgr = EmbeddingManager(cache_dir="/tmp/test_models")
        r = mgr.resolve({"id": "custom/model", "dimensions": 512})
        assert r.fastembed_id == "custom/model"
        assert r.dimensions == 512
        assert r.is_custom is True

    def test_resolve_dict_with_pooling(self):
        mgr = EmbeddingManager(cache_dir="/tmp/test_models")
        r = mgr.resolve({"id": "custom/m", "dimensions": 256, "pooling": "cls"})
        assert r.custom_pooling == "cls"

    def test_resolve_dict_missing_fields(self):
        mgr = EmbeddingManager(cache_dir="/tmp/test_models")
        with pytest.raises(ValueError, match="id.*dimensions"):
            mgr.resolve({"id": "model"})

    def test_resolve_unknown_string(self):
        mgr = EmbeddingManager(cache_dir="/tmp/test_models")
        with pytest.raises(ValueError, match="Unknown embedding model"):
            mgr.resolve("nonexistent-model-xyz")

    def test_resolve_with_dimensions_suffix(self):
        mgr = EmbeddingManager(cache_dir="/tmp/test_models")
        r = mgr.resolve("custom/model:768")
        assert r.fastembed_id == "custom/model"
        assert r.dimensions == 768

    def test_resolve_object_with_attrs(self):
        mgr = EmbeddingManager(cache_dir="/tmp/test_models")
        obj = MagicMock()
        obj.id = "my/model"
        obj.dimensions = 512
        r = mgr.resolve(obj)
        assert r.fastembed_id == "my/model"
        assert r.dimensions == 512

    def test_caching_resolved(self):
        mgr = EmbeddingManager(cache_dir="/tmp/test_models")
        r1 = mgr.resolve("minilm-l12")
        r2 = mgr.resolve("minilm-l12")
        assert r1 is r2

    def test_list_builtin(self):
        models = EmbeddingManager.list_builtin()
        assert "minilm-l12" in models
        assert "bge-m3" in models
        assert models["minilm-l12"]["dimensions"] == 384


# =========================================================================
# Reranker resolution
# =========================================================================

from digitorn.modules.rag.reranker import BUILTIN_RERANKERS, CrossEncoderReranker


class TestRerankerResolution:
    def test_resolve_builtin(self):
        for shortcut, expected_id in BUILTIN_RERANKERS.items():
            assert CrossEncoderReranker._resolve(shortcut) == expected_id

    def test_resolve_custom(self):
        assert CrossEncoderReranker._resolve("custom/model") == "custom/model"

    def test_list_builtin(self):
        models = CrossEncoderReranker.list_builtin()
        assert "minilm-l6" in models

    def test_rerank_empty_docs(self):
        r = CrossEncoderReranker.__new__(CrossEncoderReranker)
        result = r.rerank("query", [])
        assert result == []


# =========================================================================
# Ingestors (pure Python — sync, file-based)
# =========================================================================

from digitorn.modules.rag.indexing.ingestors import (
    CSVIngestor,
    CodeIngestor,
    HTMLIngestor,
    IngestDocument,
    JSONIngestor,
    JSONLIngestor,
    MarkdownIngestor,
    PlainTextIngestor,
    get_ingestor,
    is_async_format,
    supported_extensions,
)


class TestPlainTextIngestor:
    def test_basic(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("Hello world this is a test document.")
        docs = PlainTextIngestor().ingest(f)
        assert len(docs) == 1
        assert "Hello world" in docs[0].text
        assert docs[0].metadata["format"] == "text"

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        docs = PlainTextIngestor().ingest(f)
        assert docs == []


class TestMarkdownIngestor:
    def test_splits_by_headers(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Header 1\nContent 1\n\n## Header 2\nContent 2\n")
        docs = MarkdownIngestor().ingest(f)
        assert len(docs) >= 2
        assert docs[0].metadata["format"] == "markdown"
        assert "section" in docs[0].metadata

    def test_no_headers(self, tmp_path):
        f = tmp_path / "plain.md"
        f.write_text("Just plain text without any headers.")
        docs = MarkdownIngestor().ingest(f)
        assert len(docs) == 1

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.md"
        f.write_text("")
        docs = MarkdownIngestor().ingest(f)
        assert docs == []


class TestCodeIngestor:
    def test_python_file(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("def hello():\n    return 'world'\n")
        docs = CodeIngestor().ingest(f)
        assert len(docs) == 1
        assert docs[0].metadata["language"] == "python"
        assert docs[0].metadata["format"] == "code"

    def test_unknown_extension(self, tmp_path):
        f = tmp_path / "test.xyz"
        f.write_text("some content")
        docs = CodeIngestor().ingest(f)
        assert len(docs) == 1
        assert docs[0].metadata["language"] == "unknown"

    def test_empty_code(self, tmp_path):
        f = tmp_path / "empty.py"
        f.write_text("")
        docs = CodeIngestor().ingest(f)
        assert docs == []


class TestCSVIngestor:
    def test_basic_csv(self, tmp_path):
        f = tmp_path / "test.csv"
        f.write_text("name,age,city\nAlice,30,Paris\nBob,25,London\n")
        docs = CSVIngestor().ingest(f)
        assert len(docs) == 2
        assert "Alice" in docs[0].text
        assert docs[0].metadata["format"] == "csv"
        assert docs[0].metadata["row_index"] == 0

    def test_max_rows(self, tmp_path):
        f = tmp_path / "big.csv"
        lines = ["col1,col2"] + [f"val{i},data{i}" for i in range(100)]
        f.write_text("\n".join(lines))
        docs = CSVIngestor().ingest(f, max_rows=10)
        assert len(docs) == 10


class TestJSONIngestor:
    def test_object(self, tmp_path):
        f = tmp_path / "test.json"
        f.write_text(json.dumps({"key": "value", "nested": {"a": 1}}))
        docs = JSONIngestor().ingest(f)
        assert len(docs) == 1
        assert docs[0].metadata["format"] == "json"

    def test_array(self, tmp_path):
        f = tmp_path / "test.json"
        f.write_text(json.dumps([{"name": "Alice"}, {"name": "Bob"}]))
        docs = JSONIngestor().ingest(f)
        assert len(docs) == 2

    def test_invalid_json(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{invalid json content")
        docs = JSONIngestor().ingest(f)
        assert len(docs) == 1  # falls back to raw text


class TestJSONLIngestor:
    def test_basic(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text('{"a": 1}\n{"b": 2}\n\n{"c": 3}\n')
        docs = JSONLIngestor().ingest(f)
        assert len(docs) == 3
        assert docs[0].metadata["format"] == "jsonl"


class TestHTMLIngestor:
    def test_strips_tags(self, tmp_path):
        f = tmp_path / "test.html"
        f.write_text("<html><body><h1>Title</h1><p>Content here.</p></body></html>")
        docs = HTMLIngestor().ingest(f)
        assert len(docs) == 1
        assert "<" not in docs[0].text
        assert "Title" in docs[0].text
        assert "Content here" in docs[0].text

    def test_empty_html(self, tmp_path):
        f = tmp_path / "empty.html"
        f.write_text("<html><body></body></html>")
        docs = HTMLIngestor().ingest(f)
        assert docs == []


class TestIngestorRegistry:
    def test_get_ingestor(self):
        assert get_ingestor(".md") is not None
        assert get_ingestor(".txt") is not None
        assert get_ingestor(".py") is not None
        assert get_ingestor(".csv") is not None
        assert get_ingestor(".json") is not None
        assert get_ingestor(".html") is not None

    def test_get_unknown(self):
        assert get_ingestor(".xyz") is None

    def test_is_async_format(self):
        assert is_async_format(".pdf") is True
        assert is_async_format(".xlsx") is True
        assert is_async_format(".md") is False

    def test_supported_extensions(self):
        exts = supported_extensions()
        assert ".md" in exts
        assert ".py" in exts
        assert ".pdf" in exts
        assert ".csv" in exts


# =========================================================================
# IndexingEngine
# =========================================================================

from digitorn.modules.rag.indexing.engine import IndexingEngine


class TestIndexingEngine:
    async def test_scan_directory(self, tmp_path):
        (tmp_path / "doc1.txt").write_text("Hello world document one")
        (tmp_path / "doc2.txt").write_text("Goodbye world document two")
        (tmp_path / "ignore.xyz").write_text("Should be ignored")

        engine = IndexingEngine()
        docs = await engine.scan_directory(str(tmp_path), [".txt"])
        assert len(docs) == 2
        texts = [d.text for d in docs]
        assert any("Hello" in t for t in texts)

    async def test_incremental_skip(self, tmp_path):
        (tmp_path / "doc.txt").write_text("Content")
        engine = IndexingEngine()

        docs1 = await engine.scan_directory(str(tmp_path), [".txt"])
        assert len(docs1) == 1

        # Second scan: same content → skip
        docs2 = await engine.scan_directory(str(tmp_path), [".txt"])
        assert len(docs2) == 0

    async def test_rescan_on_change(self, tmp_path):
        f = tmp_path / "doc.txt"
        f.write_text("Original content")
        engine = IndexingEngine()

        docs1 = await engine.scan_directory(str(tmp_path), [".txt"])
        assert len(docs1) == 1

        f.write_text("Modified content")
        docs2 = await engine.scan_directory(str(tmp_path), [".txt"])
        assert len(docs2) == 1

    async def test_scan_nonexistent_directory(self):
        engine = IndexingEngine()
        docs = await engine.scan_directory("/nonexistent/path", [".txt"])
        assert docs == []

    async def test_max_files_limit(self, tmp_path):
        for i in range(20):
            (tmp_path / f"doc{i:02d}.txt").write_text(f"Content {i}")
        engine = IndexingEngine()
        docs = await engine.scan_directory(str(tmp_path), [".txt"], max_files=5)
        assert len(docs) == 5

    async def test_recursive_scan(self, tmp_path):
        sub = tmp_path / "subdir"
        sub.mkdir()
        (tmp_path / "top.txt").write_text("Top level")
        (sub / "nested.txt").write_text("Nested level")

        engine = IndexingEngine()
        docs = await engine.scan_directory(str(tmp_path), [".txt"], recursive=True)
        assert len(docs) == 2

    async def test_non_recursive_scan(self, tmp_path):
        sub = tmp_path / "subdir"
        sub.mkdir()
        (tmp_path / "top.txt").write_text("Top level")
        (sub / "nested.txt").write_text("Nested level")

        engine = IndexingEngine()
        docs = await engine.scan_directory(str(tmp_path), [".txt"], recursive=False)
        assert len(docs) == 1

    async def test_on_file_changed_modified(self, tmp_path):
        f = tmp_path / "doc.txt"
        f.write_text("Initial content")
        engine = IndexingEngine()

        docs = await engine.on_file_changed(str(f), "modified")
        assert len(docs) == 1

    async def test_on_file_changed_deleted(self, tmp_path):
        engine = IndexingEngine()
        docs = await engine.on_file_changed("/tmp/gone.txt", "deleted")
        assert docs == []

    async def test_on_file_changed_no_duplicate(self, tmp_path):
        f = tmp_path / "doc.txt"
        f.write_text("Content")
        engine = IndexingEngine()

        docs1 = await engine.on_file_changed(str(f), "modified")
        assert len(docs1) == 1

        # Same content again
        docs2 = await engine.on_file_changed(str(f), "modified")
        assert len(docs2) == 0

    def test_state_roundtrip(self):
        engine = IndexingEngine()
        engine._content_hashes = {"file1": "hash1", "file2": "hash2"}

        snap = engine.state_snapshot()
        new_engine = IndexingEngine()
        new_engine.restore_state(snap)

        assert new_engine._content_hashes == {"file1": "hash1", "file2": "hash2"}

    async def test_ingest_single_file(self, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("# Title\nSome content here")
        engine = IndexingEngine()
        docs = await engine.ingest_single_file(f)
        assert len(docs) >= 1

    async def test_ingest_single_file_nonexistent(self):
        engine = IndexingEngine()
        docs = await engine.ingest_single_file("/nonexistent/file.txt")
        assert docs == []

    async def test_hidden_files_skipped(self, tmp_path):
        (tmp_path / ".hidden.txt").write_text("secret")
        (tmp_path / "visible.txt").write_text("public")
        engine = IndexingEngine()
        docs = await engine.scan_directory(str(tmp_path), [".txt"])
        assert len(docs) == 1
        assert ".hidden" not in docs[0].doc_id


# =========================================================================
# Sync strategies
# =========================================================================

from digitorn.modules.rag.indexing.sync import (
    ChangelogSync,
    ChangeRecord,
    NotifySync,
    UpdatedAtSync,
    create_sync,
)


class TestSyncStrategies:
    def test_create_sync_updated_at(self):
        sync = create_sync("updated_at")
        assert isinstance(sync, UpdatedAtSync)

    def test_create_sync_changelog(self):
        sync = create_sync("changelog")
        assert isinstance(sync, ChangelogSync)

    def test_create_sync_notify(self):
        sync = create_sync("notify")
        assert isinstance(sync, NotifySync)

    def test_create_sync_default(self):
        sync = create_sync("unknown")
        assert isinstance(sync, UpdatedAtSync)


class TestUpdatedAtSync:
    def test_initial_watermark(self):
        sync = UpdatedAtSync()
        assert sync.get_watermark() == "1970-01-01T00:00:00Z"

    def test_set_watermark(self):
        sync = UpdatedAtSync()
        sync.set_watermark("2024-01-01T00:00:00Z")
        assert sync.get_watermark() == "2024-01-01T00:00:00Z"

    def test_state_roundtrip(self):
        sync = UpdatedAtSync()
        sync.set_watermark("2024-06-15T12:00:00Z")
        data = sync.to_dict()

        new_sync = UpdatedAtSync()
        new_sync.restore(data)
        assert new_sync.get_watermark() == "2024-06-15T12:00:00Z"

    async def test_poll_no_bus(self):
        sync = UpdatedAtSync()
        bus = MagicMock()
        bus.call = AsyncMock(return_value=None)
        changes = await sync.poll(bus, "conn", "table", ["id", "name"])
        assert changes == []


class TestChangelogSync:
    def test_initial_watermark(self):
        sync = ChangelogSync()
        assert sync.get_watermark() == 0

    def test_set_watermark(self):
        sync = ChangelogSync()
        sync.set_watermark(42)
        assert sync.get_watermark() == 42

    def test_state_roundtrip(self):
        sync = ChangelogSync()
        sync.set_watermark(100)
        data = sync.to_dict()

        new_sync = ChangelogSync()
        new_sync.restore(data)
        assert new_sync.get_watermark() == 100


class TestNotifySync:
    def test_pending_tracking(self):
        sync = NotifySync()
        assert not sync.has_pending("users")

        sync.on_notify("users")
        assert sync.has_pending("users")

        sync.clear_pending("users")
        assert not sync.has_pending("users")

    def test_delegates_to_updated_at(self):
        sync = NotifySync()
        # Should expose watermark from inner UpdatedAtSync
        assert sync.get_watermark() == "1970-01-01T00:00:00Z"
        sync.set_watermark("2024-01-01T00:00:00Z")
        assert sync.get_watermark() == "2024-01-01T00:00:00Z"


# =========================================================================
# Backends — base protocol check
# =========================================================================

from digitorn.modules.rag.backends.base import (
    CollectionInfo,
    Document,
    SearchResult,
    VectorBackend,
)


class TestBackendBase:
    def test_collection_info(self):
        c = CollectionInfo(name="test", dimension=384)
        assert c.name == "test"
        assert c.dimension == 384
        assert c.doc_count == 0

    def test_document(self):
        d = Document(doc_id="d1", text="hello")
        assert d.doc_id == "d1"
        assert d.metadata == {}

    def test_search_result(self):
        r = SearchResult(doc_id="d1", text="hello", score=0.95)
        assert r.score == 0.95

    def test_default_get_all_raises(self):
        class MinimalBackend(VectorBackend):
            async def initialize(self): pass
            async def close(self): pass
            async def create_collection(self, name, dimension, metadata=None): pass
            async def delete_collection(self, name): return True
            async def list_collections(self): return []
            async def collection_exists(self, name): return False
            async def upsert(self, collection, ids, vectors, texts, metadatas=None): return 0
            async def delete(self, collection, ids): return 0
            async def get(self, collection, ids): return []
            async def count(self, collection): return 0
            async def search(self, collection, vector, top_k=10, min_score=0.0, filter=None): return []

        backend = MinimalBackend()
        with pytest.raises(NotImplementedError):
            from _test_helpers import run_coro
            run_coro(backend.get_all("test"))

    def test_default_hybrid_search_raises(self):
        class MinimalBackend(VectorBackend):
            async def initialize(self): pass
            async def close(self): pass
            async def create_collection(self, name, dimension, metadata=None): pass
            async def delete_collection(self, name): return True
            async def list_collections(self): return []
            async def collection_exists(self, name): return False
            async def upsert(self, collection, ids, vectors, texts, metadatas=None): return 0
            async def delete(self, collection, ids): return 0
            async def get(self, collection, ids): return []
            async def count(self, collection): return 0
            async def search(self, collection, vector, top_k=10, min_score=0.0, filter=None): return []

        backend = MinimalBackend()
        with pytest.raises(NotImplementedError):
            from _test_helpers import run_coro
            run_coro(backend.hybrid_search("test", [0.1], "query"))


# =========================================================================
# Qdrant backend (mocked qdrant_client)
# =========================================================================

class TestQdrantBackend:
    def _patch_qdrant(self):
        """Patch _ensure_qdrant to return mock QdrantClient + models."""
        mock_models = MagicMock()
        mock_models.Distance.COSINE = "Cosine"
        mock_models.VectorParams.return_value = MagicMock()
        mock_models.PointStruct = MagicMock()
        mock_models.QuantizationConfig.return_value = MagicMock()
        mock_models.ScalarQuantizationConfig.return_value = MagicMock()
        mock_models.ScalarQuantization.return_value = MagicMock()
        mock_models.ScalarType.INT8 = "int8"
        mock_models.BinaryQuantizationConfig.return_value = MagicMock()
        mock_models.BinaryQuantization.return_value = MagicMock()
        MockQC = MagicMock()
        return patch(
            "digitorn.modules.rag.backends.qdrant._ensure_qdrant",
            return_value=(MockQC, mock_models),
        ), MockQC, mock_models

    async def test_initialize_and_close(self):
        patcher, MockQC, _ = self._patch_qdrant()
        mock_client = MagicMock()
        MockQC.return_value = mock_client
        with patcher:
            from digitorn.modules.rag.backends.qdrant import QdrantBackend
            backend = QdrantBackend(path=":memory:")
            await backend.initialize()
            assert backend._client is not None
            await backend.close()
            assert backend._client is None

    async def test_create_collection(self):
        patcher, MockQC, _ = self._patch_qdrant()
        mock_client = MagicMock()
        # get_collection raises → collection doesn't exist → create it
        mock_client.get_collection.side_effect = Exception("Not found")
        MockQC.return_value = mock_client
        with patcher:
            from digitorn.modules.rag.backends.qdrant import QdrantBackend
            backend = QdrantBackend()
            await backend.initialize()
            await backend.create_collection("test", 384)
            mock_client.create_collection.assert_called_once()

    async def test_create_collection_already_exists(self):
        patcher, MockQC, _ = self._patch_qdrant()
        mock_client = MagicMock()
        # get_collection succeeds → collection exists → skip creation
        mock_client.get_collection.return_value = MagicMock()
        MockQC.return_value = mock_client
        with patcher:
            from digitorn.modules.rag.backends.qdrant import QdrantBackend
            backend = QdrantBackend()
            await backend.initialize()
            await backend.create_collection("test", 384)
            mock_client.create_collection.assert_not_called()

    async def test_upsert(self):
        patcher, MockQC, _ = self._patch_qdrant()
        mock_client = MagicMock()
        MockQC.return_value = mock_client
        with patcher:
            from digitorn.modules.rag.backends.qdrant import QdrantBackend
            backend = QdrantBackend()
            await backend.initialize()
            count = await backend.upsert(
                "test", ["d1"], [[0.1, 0.2, 0.3]], ["hello"],
                [{"key": "val"}],
            )
            assert count == 1
            mock_client.upsert.assert_called_once()

    async def test_not_initialized_raises(self):
        from digitorn.modules.rag.backends.qdrant import QdrantBackend
        backend = QdrantBackend()
        with pytest.raises(RuntimeError, match="not initialized"):
            await backend.create_collection("test", 384)

    async def test_state_snapshot(self):
        patcher, MockQC, _ = self._patch_qdrant()
        mock_client = MagicMock()
        mock_client.get_collection.side_effect = Exception("Not found")
        MockQC.return_value = mock_client
        with patcher:
            from digitorn.modules.rag.backends.qdrant import QdrantBackend
            backend = QdrantBackend()
            await backend.initialize()
            await backend.create_collection("test", 384)
            snap = backend.state_snapshot()
            assert "collection_meta" in snap
            assert "test" in snap["collection_meta"]


# =========================================================================
# ChromaDB backend (mocked)
# =========================================================================

class TestChromaBackend:
    def _mock_chromadb(self):
        mock_chroma = MagicMock()
        mock_chroma.Client.return_value = MagicMock()
        mock_chroma.PersistentClient.return_value = MagicMock()
        mock_chroma.HttpClient.return_value = MagicMock()
        return mock_chroma

    async def test_initialize_memory(self):
        mock_chroma = self._mock_chromadb()
        with patch.dict("sys.modules", {"chromadb": mock_chroma}):
            from digitorn.modules.rag.backends.chroma import ChromaBackend
            backend = ChromaBackend()
            await backend.initialize()
            assert backend._client is not None
            mock_chroma.Client.assert_called_once()

    async def test_initialize_persistent(self):
        mock_chroma = self._mock_chromadb()
        with patch.dict("sys.modules", {"chromadb": mock_chroma}):
            from digitorn.modules.rag.backends.chroma import ChromaBackend
            backend = ChromaBackend(path="/tmp/chroma")
            await backend.initialize()
            mock_chroma.PersistentClient.assert_called_once_with(path="/tmp/chroma")

    async def test_create_and_list(self):
        mock_chroma = self._mock_chromadb()
        mock_coll = MagicMock()
        mock_chroma.Client.return_value.get_or_create_collection.return_value = mock_coll
        with patch.dict("sys.modules", {"chromadb": mock_chroma}):
            from digitorn.modules.rag.backends.chroma import ChromaBackend
            backend = ChromaBackend()
            await backend.initialize()
            await backend.create_collection("test", 384)
            assert "test" in backend._meta

    async def test_close(self):
        mock_chroma = self._mock_chromadb()
        with patch.dict("sys.modules", {"chromadb": mock_chroma}):
            from digitorn.modules.rag.backends.chroma import ChromaBackend
            backend = ChromaBackend()
            await backend.initialize()
            await backend.close()
            assert backend._client is None


# =========================================================================
# LanceDB backend (mocked)
# =========================================================================

class TestLanceDBBackend:
    def _mock_lancedb(self):
        mock_lance = MagicMock()
        mock_db = MagicMock()
        mock_lance.connect.return_value = mock_db
        mock_pa = MagicMock()
        return mock_lance, mock_db, mock_pa

    async def test_initialize(self):
        mock_lance, mock_db, mock_pa = self._mock_lancedb()
        with patch.dict("sys.modules", {"lancedb": mock_lance, "pyarrow": mock_pa}):
            from digitorn.modules.rag.backends.lancedb import LanceDBBackend
            backend = LanceDBBackend(path="/tmp/lance")
            await backend.initialize()
            mock_lance.connect.assert_called_once_with("/tmp/lance")

    async def test_create_collection_new(self):
        mock_lance, mock_db, mock_pa = self._mock_lancedb()
        mock_db.table_names.return_value = []
        with patch.dict("sys.modules", {"lancedb": mock_lance, "pyarrow": mock_pa}):
            from digitorn.modules.rag.backends.lancedb import LanceDBBackend
            backend = LanceDBBackend()
            await backend.initialize()
            await backend.create_collection("test", 384)
            mock_db.create_table.assert_called_once()

    async def test_close(self):
        mock_lance, mock_db, mock_pa = self._mock_lancedb()
        with patch.dict("sys.modules", {"lancedb": mock_lance, "pyarrow": mock_pa}):
            from digitorn.modules.rag.backends.lancedb import LanceDBBackend
            backend = LanceDBBackend()
            await backend.initialize()
            await backend.close()
            assert backend._db is None


# =========================================================================
# Pinecone backend (mocked)
# =========================================================================

class TestPineconeBackend:
    def _mock_pinecone(self):
        mock_mod = MagicMock()
        mock_pc = MagicMock()
        mock_mod.Pinecone.return_value = mock_pc
        mock_mod.ServerlessSpec = MagicMock()
        return mock_mod, mock_pc

    async def test_initialize(self):
        mock_mod, mock_pc = self._mock_pinecone()
        with patch.dict("sys.modules", {"pinecone": mock_mod}):
            from digitorn.modules.rag.backends.pinecone import PineconeBackend
            backend = PineconeBackend(api_key="test-key", index_name="test-idx")
            await backend.initialize()
            mock_mod.Pinecone.assert_called_once_with(api_key="test-key")
            mock_pc.Index.assert_called_once_with("test-idx")

    async def test_upsert_batching(self):
        mock_mod, mock_pc = self._mock_pinecone()
        mock_index = MagicMock()
        mock_pc.Index.return_value = mock_index
        with patch.dict("sys.modules", {"pinecone": mock_mod}):
            from digitorn.modules.rag.backends.pinecone import PineconeBackend
            backend = PineconeBackend(api_key="k", index_name="idx")
            await backend.initialize()

            ids = [f"d{i}" for i in range(150)]
            vecs = [[0.1] * 3 for _ in range(150)]
            texts = [f"text{i}" for i in range(150)]
            count = await backend.upsert("ns", ids, vecs, texts)
            assert count == 150
            assert mock_index.upsert.call_count == 2

    async def test_close(self):
        mock_mod, mock_pc = self._mock_pinecone()
        mock_pc.Index.return_value = MagicMock()
        with patch.dict("sys.modules", {"pinecone": mock_mod}):
            from digitorn.modules.rag.backends.pinecone import PineconeBackend
            backend = PineconeBackend(api_key="k", index_name="idx")
            await backend.initialize()
            await backend.close()
            assert backend._index is None


# =========================================================================
# pgvector backend (mocked asyncpg)
# =========================================================================

class TestPgvectorBackend:
    async def test_initialize(self):
        mock_pg = MagicMock()
        mock_conn = AsyncMock()
        # Create a proper async context manager for pool.acquire()
        mock_acm = AsyncMock()
        mock_acm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acm.__aexit__ = AsyncMock(return_value=False)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_acm
        mock_pool.close = AsyncMock()
        mock_pg.create_pool = AsyncMock(return_value=mock_pool)
        with patch.dict("sys.modules", {"asyncpg": mock_pg}):
            from digitorn.modules.rag.backends.pgvector import PgvectorBackend
            backend = PgvectorBackend(dsn="postgres://localhost/test")
            await backend.initialize()
            mock_pg.create_pool.assert_awaited_once()
            mock_conn.execute.assert_awaited_once()  # CREATE EXTENSION

    async def test_close(self):
        from digitorn.modules.rag.backends.pgvector import PgvectorBackend
        backend = PgvectorBackend()
        mock_pool = AsyncMock()
        backend._pool = mock_pool
        await backend.close()
        mock_pool.close.assert_awaited_once()
        assert backend._pool is None

    async def test_not_initialized(self):
        from digitorn.modules.rag.backends.pgvector import PgvectorBackend
        backend = PgvectorBackend()
        with pytest.raises(RuntimeError, match="not initialized"):
            await backend.create_collection("test", 384)

    def test_state_snapshot(self):
        from digitorn.modules.rag.backends.pgvector import PgvectorBackend
        backend = PgvectorBackend()
        backend._meta = {"test": {"dimension": 384}}
        snap = backend.state_snapshot()
        assert snap["meta"]["test"]["dimension"] == 384


# =========================================================================
# Text2SQL strategy
# =========================================================================

from digitorn.modules.rag.strategies.text2sql import Text2SQLStrategy


class TestText2SQLStrategy:
    def test_validate_sql_select(self):
        assert Text2SQLStrategy._validate_sql("SELECT * FROM users") is True

    def test_validate_sql_rejects_delete(self):
        assert Text2SQLStrategy._validate_sql("DELETE FROM users") is False

    def test_validate_sql_rejects_update(self):
        assert Text2SQLStrategy._validate_sql("UPDATE users SET name='x'") is False

    def test_validate_sql_rejects_drop(self):
        assert Text2SQLStrategy._validate_sql("DROP TABLE users") is False

    def test_validate_sql_rejects_insert(self):
        assert Text2SQLStrategy._validate_sql("INSERT INTO users VALUES (1)") is False

    def test_validate_sql_rejects_non_select(self):
        assert Text2SQLStrategy._validate_sql("CREATE TABLE x (id INT)") is False

    def test_validate_sql_case_insensitive(self):
        assert Text2SQLStrategy._validate_sql("select count(*) from users") is True
        # DELETE inside string literal doesn't match "delete " pattern (no trailing space)
        assert Text2SQLStrategy._validate_sql("SELECT * FROM users WHERE name = 'DELETE'") is True
        # But actual DELETE statement is blocked
        assert Text2SQLStrategy._validate_sql("DELETE FROM users WHERE id = 1") is False

    def test_format_rows(self):
        rows = [
            {"name": "Alice", "age": 30},
            {"name": "Bob", "age": 25},
        ]
        text = Text2SQLStrategy._format_rows(rows, "SELECT * FROM users")
        assert "Alice" in text
        assert "Bob" in text
        assert "name" in text
        assert "age" in text
        assert "SQL:" in text

    def test_format_rows_empty(self):
        assert Text2SQLStrategy._format_rows([], "SELECT 1") == ""

    async def test_retrieve_safe_sql(self):
        mock_bus = MagicMock()
        mock_bus.call = AsyncMock(return_value=MagicMock(
            success=True,
            data={"rows": [{"name": "Alice", "age": 30}]},
        ))
        mock_llm = AsyncMock(return_value="SELECT name, age FROM users LIMIT 10")

        strategy = Text2SQLStrategy(
            service_bus=mock_bus,
            connection_id="crm",
            schema_text="Table: users\n  name TEXT\n  age INT",
            llm_caller=mock_llm,
        )
        results = await strategy.retrieve("list all users")
        assert len(results) == 1
        assert "Alice" in results[0].text
        assert results[0].citation.source_type == "database"

    async def test_retrieve_unsafe_sql_blocked(self):
        mock_llm = AsyncMock(return_value="DROP TABLE users")
        strategy = Text2SQLStrategy(
            service_bus=MagicMock(),
            connection_id="crm",
            schema_text="schema",
            llm_caller=mock_llm,
        )
        results = await strategy.retrieve("delete everything")
        assert results == []


# =========================================================================
# CRAG strategy
# =========================================================================

from digitorn.modules.rag.strategies.crag import CRAGStrategy


class TestCRAGStrategy:
    async def test_filter_by_score_no_llm(self):
        inner = AsyncMock()
        inner.retrieve = AsyncMock(return_value=[
            RetrievalResult(text="good", score=0.8, doc_id="d1"),
            RetrievalResult(text="bad", score=0.2, doc_id="d2"),
        ])

        crag = CRAGStrategy(inner, confidence_threshold=0.5)
        results = await crag.retrieve("test query")
        assert len(results) == 1
        assert results[0].doc_id == "d1"

    async def test_fallback_on_empty(self):
        inner = AsyncMock()
        inner.retrieve = AsyncMock(return_value=[])

        crag = CRAGStrategy(inner, confidence_threshold=0.5, fallback="broader_query")
        results = await crag.retrieve("very specific long query words test")
        # Should attempt broader query fallback
        assert inner.retrieve.call_count >= 2

    async def test_no_fallback(self):
        inner = AsyncMock()
        inner.retrieve = AsyncMock(return_value=[])

        crag = CRAGStrategy(inner, confidence_threshold=0.5, fallback="none")
        results = await crag.retrieve("query")
        assert results == []

    async def test_llm_evaluation(self):
        inner = AsyncMock()
        inner.retrieve = AsyncMock(return_value=[
            RetrievalResult(text="relevant doc", score=0.9, doc_id="d1"),
            RetrievalResult(text="irrelevant doc", score=0.9, doc_id="d2"),
        ])

        mock_llm = AsyncMock(side_effect=["0.9", "0.1"])
        crag = CRAGStrategy(inner, llm_caller=mock_llm, confidence_threshold=0.5)
        results = await crag.retrieve("test")
        assert len(results) == 1
        assert results[0].doc_id == "d1"


# =========================================================================
# Adaptive strategy
# =========================================================================

from digitorn.modules.rag.strategies.adaptive import AdaptiveStrategy


class TestAdaptiveStrategy:
    async def test_routes_to_correct_strategy(self):
        semantic = AsyncMock()
        semantic.retrieve = AsyncMock(return_value=[
            RetrievalResult(text="semantic result", score=0.9, doc_id="d1"),
        ])
        hybrid = AsyncMock()
        hybrid.retrieve = AsyncMock(return_value=[
            RetrievalResult(text="hybrid result", score=0.8, doc_id="d2"),
        ])

        router = QueryRouter(has_sql_sources=False)
        adaptive = AdaptiveStrategy(
            strategies={"semantic": semantic, "hybrid": hybrid},
            router=router,
        )

        results = await adaptive.retrieve("what is the policy on remote work")
        assert len(results) >= 1

    async def test_fallback_to_default(self):
        hybrid = AsyncMock()
        hybrid.retrieve = AsyncMock(return_value=[
            RetrievalResult(text="result", score=0.9, doc_id="d1"),
        ])

        router = QueryRouter(has_sql_sources=True)
        adaptive = AdaptiveStrategy(
            strategies={"hybrid": hybrid},
            router=router,
            default="hybrid",
        )

        # SQL route requested but not available → should fall back to hybrid
        results = await adaptive.retrieve("combien de clients total")
        assert len(results) >= 1
        hybrid.retrieve.assert_awaited()

    async def test_empty_strategies(self):
        router = QueryRouter()
        adaptive = AdaptiveStrategy(strategies={}, router=router)
        results = await adaptive.retrieve("test")
        assert results == []


# =========================================================================
# MultiQuery strategy
# =========================================================================

from digitorn.modules.rag.strategies.multiquery import MultiQueryStrategy
from digitorn.modules.rag.config import MultiQueryConfig


class TestMultiQueryStrategy:
    async def test_heuristic_variants_no_llm(self):
        inner = AsyncMock()
        inner.retrieve = AsyncMock(return_value=[
            RetrievalResult(text="result", score=0.9, doc_id="d1"),
        ])

        cfg = MultiQueryConfig(enabled=True, num_variants=3)
        mq = MultiQueryStrategy(inner, cfg, llm_caller=None)
        results = await mq.retrieve("this is a long enough query for variants")
        # Should call inner multiple times (original + variants)
        assert inner.retrieve.call_count >= 2

    async def test_with_llm(self):
        inner = AsyncMock()
        inner.retrieve = AsyncMock(return_value=[
            RetrievalResult(text="result", score=0.9, doc_id="d1"),
        ])

        mock_llm = AsyncMock(return_value="variant 1\nvariant 2\nvariant 3")
        cfg = MultiQueryConfig(enabled=True, num_variants=3)
        mq = MultiQueryStrategy(inner, cfg, llm_caller=mock_llm)
        results = await mq.retrieve("test query")
        # original + 3 variants = 4 calls
        assert inner.retrieve.call_count == 4

    def test_heuristic_short_query(self):
        variants = MultiQueryStrategy._heuristic_variants("short")
        assert variants == []  # too short for heuristics

    def test_heuristic_long_query(self):
        variants = MultiQueryStrategy._heuristic_variants("this is a longer query")
        assert len(variants) == 2


# =========================================================================
# Pipeline (with mock backend + embeddings)
# =========================================================================

from digitorn.modules.rag.pipeline import RagPipeline


class TestRagPipeline:
    @pytest.fixture()
    def mock_backend(self):
        backend = AsyncMock()
        backend.search = AsyncMock(return_value=[
            SearchResult(doc_id="d1", text="result one", score=0.9, metadata={"source_type": "file", "source_id": "a.txt"}),
            SearchResult(doc_id="d2", text="result two", score=0.7, metadata={"source_type": "file", "source_id": "b.txt"}),
        ])
        backend.get = AsyncMock(return_value=[
            Document(doc_id="d3", text="bm25 only result", metadata={}),
        ])
        return backend

    @pytest.fixture()
    def mock_emb(self):
        mgr = MagicMock(spec=EmbeddingManager)
        mgr.embed_single = MagicMock(return_value=[0.1, 0.2, 0.3])
        return mgr

    @pytest.fixture()
    def model(self):
        return ResolvedModel(fastembed_id="test", dimensions=3)

    @pytest.fixture()
    def bm25(self):
        idx = BM25Index()
        idx.add_documents(["d1", "d2", "d3"], [
            "result one about programming",
            "result two about databases",
            "result three about cooking",
        ])
        return idx

    @pytest.fixture()
    def pipeline(self, mock_backend, mock_emb):
        cfg = PipelineConfig(retrieval="hybrid", rerank_top_n=0, final_top_k=5)
        return RagPipeline(
            backend=mock_backend,
            embedding_mgr=mock_emb,
            config=cfg,
        )

    async def test_semantic_retrieval(self, pipeline, model, bm25):
        results = await pipeline.retrieve(
            "test query", "coll", model, bm25,
            strategy="semantic", top_k=5,
        )
        assert len(results) > 0
        assert all(isinstance(r, RetrievalResult) for r in results)

    async def test_bm25_retrieval(self, pipeline, model, bm25, mock_backend):
        mock_backend.get = AsyncMock(return_value=[
            Document(doc_id="d1", text="result one", metadata={}),
        ])
        results = await pipeline.retrieve(
            "programming", "coll", model, bm25,
            strategy="bm25", top_k=5,
        )
        assert len(results) > 0

    async def test_hybrid_retrieval(self, pipeline, model, bm25):
        results = await pipeline.retrieve(
            "programming", "coll", model, bm25,
            strategy="hybrid", top_k=5,
        )
        assert len(results) > 0

    async def test_top_k_respected(self, pipeline, model, bm25):
        results = await pipeline.retrieve(
            "test", "coll", model, bm25, top_k=1,
        )
        assert len(results) <= 1

    async def test_citations_populated(self, pipeline, model, bm25):
        results = await pipeline.retrieve(
            "test", "coll", model, bm25, strategy="semantic",
        )
        for r in results:
            assert r.citation is not None
            assert r.citation.source_type in ("file", "manual", "database", "web", "api")

    async def test_streaming_retrieval(self, pipeline, model, bm25, mock_backend):
        mock_backend.get = AsyncMock(return_value=[
            Document(doc_id="d1", text="result", metadata={}),
        ])
        fast, late = await pipeline.retrieve_streaming(
            "test", "coll", model, bm25, top_k=5, timeout=1.0,
        )
        assert isinstance(fast, list)
        assert isinstance(late, list)

    def test_deduplicate(self):
        results = [
            RetrievalResult(text="a", score=0.9, doc_id="d1"),
            RetrievalResult(text="b", score=0.8, doc_id="d1"),
            RetrievalResult(text="c", score=0.7, doc_id="d2"),
        ]
        deduped = RagPipeline._deduplicate(results)
        assert len(deduped) == 2
        assert deduped[0].doc_id == "d1"
        assert deduped[1].doc_id == "d2"


# =========================================================================
# RagModule — full action tests (mocked backend + embeddings)
# =========================================================================

from digitorn.modules.rag.module import RagModule


class _MockBackend:
    """In-memory vector backend for testing the module end-to-end."""

    supports_sparse = False
    supports_hybrid = False
    supports_metadata_filter = True

    def __init__(self):
        self._collections: dict[str, dict[str, Any]] = {}
        self._data: dict[str, list[dict[str, Any]]] = {}

    async def initialize(self):
        pass

    async def close(self):
        pass

    async def create_collection(self, name, dimension, metadata=None):
        self._collections[name] = {"dimension": dimension, **(metadata or {})}
        if name not in self._data:
            self._data[name] = []

    async def delete_collection(self, name):
        self._collections.pop(name, None)
        self._data.pop(name, None)
        return True

    async def list_collections(self):
        return [
            CollectionInfo(name=n, dimension=m.get("dimension", 0))
            for n, m in self._collections.items()
        ]

    async def collection_exists(self, name):
        return name in self._collections

    async def upsert(self, collection, ids, vectors, texts, metadatas=None):
        if collection not in self._data:
            self._data[collection] = []
        for i, (doc_id, vec, text) in enumerate(zip(ids, vectors, texts)):
            meta = metadatas[i] if metadatas and i < len(metadatas) else {}
            # Remove existing with same id
            self._data[collection] = [d for d in self._data[collection] if d["id"] != doc_id]
            self._data[collection].append({
                "id": doc_id, "vector": vec, "text": text, "metadata": meta,
            })
        return len(ids)

    async def delete(self, collection, ids):
        if collection not in self._data:
            return 0
        before = len(self._data[collection])
        self._data[collection] = [d for d in self._data[collection] if d["id"] not in ids]
        return before - len(self._data[collection])

    async def get(self, collection, ids):
        if collection not in self._data:
            return []
        return [
            Document(doc_id=d["id"], text=d["text"], metadata=d["metadata"])
            for d in self._data[collection] if d["id"] in ids
        ]

    async def count(self, collection):
        return len(self._data.get(collection, []))

    async def get_all(self, collection):
        return [
            {"id": d["id"], "text": d["text"], "metadata": d["metadata"]}
            for d in self._data.get(collection, [])
        ]

    async def search(self, collection, vector, top_k=10, min_score=0.0, filter=None):
        if collection not in self._data:
            return []
        results = []
        for d in self._data[collection]:
            score = 0.9 - len(results) * 0.1
            if score >= min_score:
                results.append(SearchResult(
                    doc_id=d["id"], text=d["text"], score=score, metadata=d["metadata"],
                ))
        return results[:top_k]

    def state_snapshot(self):
        return {}

    async def restore_state(self, state):
        pass


class TestRagModuleActions:
    @pytest.fixture()
    async def module(self):
        mod = RagModule()
        mock_backend = _MockBackend()
        mock_emb = MagicMock(spec=EmbeddingManager)
        mock_emb.resolve = MagicMock(return_value=ResolvedModel("test-model", 3))
        mock_emb.embed = MagicMock(return_value=[[0.1, 0.2, 0.3]])
        mock_emb.embed_single = MagicMock(return_value=[0.1, 0.2, 0.3])

        mod._cfg = RagConfig()
        mod._embedding_mgr = mock_emb
        mod._default_model = ResolvedModel("test-model", 3)
        mod._backend = mock_backend
        mod._pipeline = RagPipeline(
            backend=mock_backend, embedding_mgr=mock_emb,
            config=PipelineConfig(rerank_top_n=0),
        )
        mod._cache = None
        mod._router = QueryRouter()
        mod._indexing = IndexingEngine()
        mod._kbs = {}
        mod._app_id = "test"
        return mod

    async def test_create_knowledge_base(self, module):
        result = await module.create_knowledge_base(
            CreateKnowledgeBaseParams(name="docs", description="Test KB"),
        )
        assert result.success is True
        assert result.data["knowledge_base"] == "docs"
        assert "docs" in module._kbs

    async def test_create_duplicate_kb(self, module):
        await module.create_knowledge_base(
            CreateKnowledgeBaseParams(name="docs"),
        )
        result = await module.create_knowledge_base(
            CreateKnowledgeBaseParams(name="docs"),
        )
        assert result.success is True
        assert result.data["created"] is False
        assert result.data["already_exists"] is True

    async def test_delete_knowledge_base(self, module):
        await module.create_knowledge_base(
            CreateKnowledgeBaseParams(name="docs"),
        )
        result = await module.delete_knowledge_base(
            DeleteKnowledgeBaseParams(name="docs"),
        )
        assert result.success is True
        assert "docs" not in module._kbs

    async def test_delete_nonexistent_kb(self, module):
        result = await module.delete_knowledge_base(
            DeleteKnowledgeBaseParams(name="nope"),
        )
        assert result.success is False

    async def test_list_knowledge_bases(self, module):
        await module.create_knowledge_base(
            CreateKnowledgeBaseParams(name="kb1"),
        )
        await module.create_knowledge_base(
            CreateKnowledgeBaseParams(name="kb2"),
        )
        result = await module.list_knowledge_bases(ListKnowledgeBasesParams())
        assert result.success is True
        assert len(result.data["knowledge_bases"]) == 2

    async def test_knowledge_base_stats(self, module):
        await module.create_knowledge_base(
            CreateKnowledgeBaseParams(name="docs"),
        )
        result = await module.knowledge_base_stats(
            KnowledgeBaseStatsParams(name="docs"),
        )
        assert result.success is True
        assert result.data["name"] == "docs"
        assert "pipeline" in result.data
        assert "backend_type" in result.data

    async def test_ingest(self, module):
        await module.create_knowledge_base(
            CreateKnowledgeBaseParams(name="docs"),
        )
        result = await module.ingest(IngestParams(
            knowledge_base="docs",
            documents=["First document about AI", "Second document about ML"],
        ))
        assert result.success is True
        assert result.data["added"] == 2
        assert module._kbs["docs"].chunk_count == 2

    async def test_ingest_nonexistent_kb(self, module):
        result = await module.ingest(IngestParams(
            knowledge_base="nope",
            documents=["doc"],
        ))
        assert result.success is False

    async def test_query(self, module):
        await module.create_knowledge_base(
            CreateKnowledgeBaseParams(name="docs"),
        )
        await module.ingest(IngestParams(
            knowledge_base="docs",
            documents=["Python is a programming language", "Java is also popular"],
        ))
        result = await module.query(QueryParams(
            knowledge_base="docs",
            query="programming language",
        ))
        assert result.success is True
        assert len(result.data["results"]) > 0

    async def test_query_nonexistent_kb(self, module):
        result = await module.query(QueryParams(
            knowledge_base="nope", query="test",
        ))
        assert result.success is False

    async def test_query_with_strategy(self, module):
        await module.create_knowledge_base(
            CreateKnowledgeBaseParams(name="docs"),
        )
        await module.ingest(IngestParams(
            knowledge_base="docs",
            documents=["Test doc"],
        ))
        for strategy in ("semantic", "bm25", "hybrid"):
            result = await module.query(QueryParams(
                knowledge_base="docs", query="test", strategy=strategy,
            ))
            assert result.success is True

    async def test_clear_cache_no_cache(self, module):
        result = await module.clear_cache(ClearCacheParams())
        assert result.success is True
        assert result.data["cache_enabled"] is False

    async def test_list_models(self, module):
        result = await module.list_models(ListModelsParams())
        assert result.success is True
        assert len(result.data["models"]) >= 7
        shortcuts = [m["shortcut"] for m in result.data["models"]]
        assert "minilm-l12" in shortcuts
        assert "bge-m3" in shortcuts

    async def test_ingest_file(self, module, tmp_path):
        await module.create_knowledge_base(
            CreateKnowledgeBaseParams(name="docs"),
        )
        f = tmp_path / "test.txt"
        f.write_text("This is a test document for ingestion.")
        result = await module.ingest_file(IngestFileParams(
            knowledge_base="docs", path=str(f),
        ))
        assert result.success is True
        assert result.data["chunks"] > 0

    async def test_ingest_file_nonexistent(self, module):
        await module.create_knowledge_base(
            CreateKnowledgeBaseParams(name="docs"),
        )
        result = await module.ingest_file(IngestFileParams(
            knowledge_base="docs", path="/nonexistent/file.txt",
        ))
        assert result.success is False

    async def test_migrate_embeddings(self, module):
        await module.create_knowledge_base(
            CreateKnowledgeBaseParams(name="docs"),
        )
        await module.ingest(IngestParams(
            knowledge_base="docs",
            documents=["Test document for migration"],
        ))

        target = ResolvedModel(fastembed_id="new-model", dimensions=5)
        module._embedding_mgr.resolve = MagicMock(return_value=target)
        module._embedding_mgr.embed = MagicMock(return_value=[[0.1, 0.2, 0.3, 0.4, 0.5]])

        result = await module.migrate_embeddings(MigrateEmbeddingsParams(
            knowledge_base="docs", target_model="new-model",
        ))
        assert result.success is True
        assert result.data["migrated"] is True
        assert result.data["old_model"] == "test-model"
        assert result.data["new_model"] == "new-model"

    async def test_migrate_same_model(self, module):
        await module.create_knowledge_base(
            CreateKnowledgeBaseParams(name="docs"),
        )
        module._embedding_mgr.resolve = MagicMock(
            return_value=ResolvedModel("test-model", 3),
        )
        result = await module.migrate_embeddings(MigrateEmbeddingsParams(
            knowledge_base="docs", target_model="test-model",
        ))
        assert result.success is True
        assert result.data["migrated"] is False

    async def test_state_snapshot_and_restore(self, module):
        await module.create_knowledge_base(
            CreateKnowledgeBaseParams(name="docs", description="Test"),
        )
        await module.ingest(IngestParams(
            knowledge_base="docs", documents=["test"],
        ))

        snap = module.state_snapshot()
        assert "knowledge_bases" in snap
        assert "docs" in snap["knowledge_bases"]

        # Restore into new module
        mod2 = RagModule()
        mod2._backend = _MockBackend()
        mod2._indexing = IndexingEngine()
        mod2._cache = None
        await mod2.restore_state(snap)
        assert "docs" in mod2._kbs
        assert mod2._kbs["docs"].description == "Test"

    async def test_ingest_directory(self, module, tmp_path):
        await module.create_knowledge_base(
            CreateKnowledgeBaseParams(name="docs"),
        )
        (tmp_path / "a.txt").write_text("Document alpha content")
        (tmp_path / "b.txt").write_text("Document beta content")
        (tmp_path / "c.pdf").write_text("Ignored PDF")

        result = await module.ingest_directory(IngestDirectoryParams(
            knowledge_base="docs",
            path=str(tmp_path),
            extensions=[".txt"],
        ))
        assert result.success is True
        assert result.data["documents"] == 2

    async def test_query_with_citations(self, module):
        module._cfg.citations.enabled = True
        await module.create_knowledge_base(
            CreateKnowledgeBaseParams(name="docs"),
        )
        await module.ingest(IngestParams(
            knowledge_base="docs",
            documents=["Important policy document content"],
            source_type="file",
            source_id="policy.txt",
        ))
        result = await module.query(QueryParams(
            knowledge_base="docs", query="policy",
        ))
        assert result.success is True
        if result.data["results"]:
            r = result.data["results"][0]
            assert "citation" in r

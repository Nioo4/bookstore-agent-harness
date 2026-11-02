from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.config import RagCommandSettings
from app.models.embeddings import EmbeddingResponse, EmbeddingProvider
from app.rag.contracts import RagDocument, RagDocumentLoader, RagManifest
from app.rag.evidence import evidence_from_chunk, validate_book_evidence, validate_policy_evidence
from app.rag.evaluation import ChunkingObservation, select_chunking, select_threshold, RetrievalObservation
from app.rag.indexer import RAGIndexer
from app.rag.qdrant import QdrantRagStore
from app.rag_index import _read_tuning_report, _resolve_build_options, _resolve_tuning_dataset
from app.rag_evaluation import RagEvalCase, _live_evaluate, _parser, evaluate_fixture, load_cases


def _policy_chunk() -> dict[str, object]:
    document = RagDocument.model_validate(
        {
            "document_id": "policy-hardening",
            "document_type": "POLICY",
            "title": "Hardening policy",
            "version": "2.0",
            "effective_date": "2026-09-25",
            "locale": "zh-CN",
            "source_type": "CURATED_INTERNAL",
            "source_uri": "knowledge/policies/hardening.md",
            "body": "## Confirmation\nUse an explicit confirmation.",
        }
    )
    chunk = RagDocumentLoader(target_tokens=256, overlap_tokens=32).chunks_for(document)[0]
    return chunk.with_embedding_space("space-test").model_dump(mode="json")


def test_evidence_is_bound_to_real_chunk_hash_point_and_metadata() -> None:
    chunk = _policy_chunk()
    result = evidence_from_chunk(chunk)
    assert result["evidence_id"] == "policy:" + str(chunk["content_hash"])
    assert result["locale"] == "zh-CN"
    assert result["source_type"] == "CURATED_INTERNAL"
    assert result["catalog_ref"] is None
    assert result["chunk_index"] == chunk["chunk_index"]
    assert result["point_id"] == chunk["point_id"]
    assert result["embedding_space"] == "space-test"
    validate_policy_evidence(result)
    validate_policy_evidence(
        chunk,
        source_uri="knowledge/policies/hardening.md",
        section_path="Confirmation",
        version="2.0",
    )
    tampered = dict(chunk, content="forged text")
    with pytest.raises(ValueError, match="content_hash"):
        evidence_from_chunk(tampered)
    with pytest.raises(ValueError, match="source_uri"):
        validate_policy_evidence(chunk, source_uri="knowledge/policies/other.md")


def test_book_evidence_must_be_in_current_catalog_set() -> None:
    chunk = _policy_chunk()
    chunk.update(
        {
            "document_id": "book-guide-b31",
            "document_type": "BOOK_GUIDE",
            "catalog_ref": "B31",
            "book_id": 31,
            "isbn": "9780000000031",
            "source_type": "CURATED_SYNTHETIC",
        }
    )
    # Recompute the stable point ID because the document identity changed.
    from app.rag.contracts import chunk_point_id

    chunk["point_id"] = chunk_point_id(str(chunk["document_id"]), str(chunk["section_path"]), int(chunk["chunk_index"]), str(chunk["content_hash"]))
    validate_book_evidence(chunk, [31])
    with pytest.raises(ValueError, match="outside"):
        validate_book_evidence(chunk, [32])


@pytest.mark.asyncio
async def test_qdrant_404_is_dependency_error_but_200_empty_is_no_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/missing/points/query"):
            return httpx.Response(404, request=request, json={"status": {"error": "missing"}})
        return httpx.Response(200, request=request, json={"result": {"points": []}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://qdrant.test")
    missing = QdrantRagStore("http://qdrant.test", alias="missing", dimensions=2, client=client)
    empty = QdrantRagStore("http://qdrant.test", alias="empty", dimensions=2, client=client)
    with pytest.raises(httpx.HTTPStatusError):
        await missing.query([0.1, 0.2], limit=5, score_threshold=0.2)
    assert await empty.query([0.1, 0.2], limit=5, score_threshold=0.2) == []
    await client.aclose()


@pytest.mark.asyncio
async def test_qdrant_audit_normalizes_returned_uuid_and_checks_payload_identity() -> None:
    canonical = "0886b8d43e0e87140a4d5695afc105b8"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "result": {
                    "points": [
                        {
                            "id": "0886b8d4-3e0e-8714-0a4d-5695afc105b8",
                            "payload": {"point_id": canonical, "document_id": "policy-a"},
                        }
                    ],
                    "next_page_offset": None,
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://qdrant.test")
    store = QdrantRagStore("http://qdrant.test", alias="policy", dimensions=2, client=client)
    assert await store.scroll_payloads("physical-policy") == {
        canonical: {"point_id": canonical, "document_id": "policy-a"}
    }
    await client.aclose()


@pytest.mark.asyncio
async def test_qdrant_query_normalizes_uuid_and_preserves_payload_identity() -> None:
    canonical = "00112233445566778899aabbccddeeff"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/collections/policy/points/query"
        return httpx.Response(
            200,
            request=request,
            json={
                "result": {
                    "points": [
                        {
                            "id": "00112233-4455-6677-8899-aabbccddeeff",
                            "score": .9,
                            "payload": {"point_id": canonical, "document_id": "policy-a"},
                        }
                    ]
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://qdrant.test")
    store = QdrantRagStore("http://qdrant.test", alias="policy", dimensions=2, client=client)
    assert await store.query([.1, .2], limit=5, score_threshold=.2) == [
        {"point_id": canonical, "document_id": "policy-a", "score": .9}
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_qdrant_alias_reads_use_the_global_alias_endpoint() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/collections/physical-policy":
            return httpx.Response(
                200,
                request=request,
                json={"result": {"config": {"params": {"vectors": {"size": 2, "distance": "Cosine"}}}}},
            )
        if request.method == "GET" and request.url.path == "/aliases":
            return httpx.Response(
                200,
                request=request,
                json={"result": {"aliases": [{"alias_name": "policy", "collection_name": "physical-policy"}]}},
            )
        if request.method == "POST" and request.url.path == "/collections/aliases":
            return httpx.Response(200, request=request, json={"status": "ok"})
        return httpx.Response(404, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://qdrant.test")
    store = QdrantRagStore("http://qdrant.test", alias="policy", dimensions=2, client=client)
    await store.assert_alias_compatible("physical-policy")
    await store.swap_alias("physical-policy")
    assert ("GET", "/aliases") in seen
    assert ("GET", "/collections/aliases") not in seen
    assert ("POST", "/collections/aliases") in seen
    await client.aclose()


def test_threshold_and_chunk_tie_breaks_are_deterministic() -> None:
    selected = select_threshold(
        [
            RetrievalObservation(("p",), (0.8,), frozenset({"p"})),
            RetrievalObservation(("x",), (0.6,), frozenset(), True),
        ]
    )
    assert selected.passed and selected.threshold == 0.8
    chunk = select_chunking(
        [
            ChunkingObservation(500, 80, 0.95, 0.80, 100),
            ChunkingObservation(384, 48, 0.95, 0.80, 90),
            ChunkingObservation(256, 32, 0.95, 0.80, 90),
        ]
    )
    assert (chunk.target_tokens, chunk.overlap_tokens) == (256, 32)


def test_live_threshold_selection_requires_one_expected_evidence_section_per_case() -> None:
    from app.rag_evaluation import (
        RagEvalCase,
        RetrievedChunk,
        _aggregate_chunk_hits,
        _select_threshold_with_section_gate,
    )

    cases = [
        RagEvalCase(
            case_id="book-sections",
            corpus="BOOK_GUIDE",
            query="入门和前置知识",
            expected_document_ids=["book-guide-b25"],
            expected_sections=["适读人群", "前置知识"],
        ),
        RagEvalCase(
            case_id="book-no-answer",
            corpus="BOOK_GUIDE",
            query="不存在的主题",
            expected_document_ids=[],
            expected_sections=[],
            must_abstain=True,
        ),
    ]
    chunks = {
        "book-sections": [
            RetrievedChunk("book-guide-b25", "适读人群", "B25", .90),
            RetrievedChunk("book-guide-b25", "前置知识", "B25", .70),
        ],
        "book-no-answer": [RetrievedChunk("book-guide-b26", "核心主题", "B26", .60)],
    }
    observations = {
        case.case_id: _aggregate_chunk_hits(case, chunks[case.case_id])
        for case in cases
    }

    choice, section_recall = _select_threshold_with_section_gate(cases, observations, chunks)

    assert choice.passed is True
    assert choice.threshold == .90
    assert choice.recall_at_5 == 1.0
    assert choice.false_positive_rate == 0.0
    assert section_recall == 1.0


def test_duplicate_h2_with_identical_content_is_rejected_before_indexing() -> None:
    document = RagDocument.model_validate(
        {
            "document_id": "policy-duplicate-h2",
            "document_type": "POLICY",
            "title": "Duplicate",
            "version": "1",
            "effective_date": "2026-09-25",
            "locale": "zh-CN",
            "source_type": "CURATED_INTERNAL",
            "source_uri": "duplicate.md",
            "body": "## Same\nThe same body.\n## Same\nThe same body.",
        }
    )
    with pytest.raises(ValueError, match="duplicate chunk point_id"):
        RagDocumentLoader(target_tokens=256, overlap_tokens=32).chunks_for(document)


def test_manifest_rejects_zero_threshold() -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        RagManifest.build(
            build_id="hardening",
            embedding_model="qwen3.7-text-embedding",
            embedding_dimensions=2,
            target_tokens=256,
            overlap_tokens=32,
            policy_threshold=0.0,
            book_threshold=0.2,
            policy_alias="policy-current",
            book_alias="book-current",
            chunks=[RagDocumentLoader(target_tokens=256, overlap_tokens=32).chunks_for(RagDocument.model_validate({
                "document_id": "policy-hardening", "document_type": "POLICY", "title": "Hardening", "version": "1",
                "effective_date": "2026-09-25", "locale": "zh-CN", "source_type": "CURATED_INTERNAL", "source_uri": "x",
                "body": "## Rules\ntext",
            }))[0]],
        )


def test_rag_fixture_requires_every_case(tmp_path: Path) -> None:
    case_file = tmp_path / "cases.jsonl"
    case_file.write_text(json.dumps({
        "case_id": "policy-1", "corpus": "POLICY", "query": "规则", "expected_document_ids": ["policy-a"], "expected_sections": ["Rules"]
    }) + "\n", encoding="utf-8")
    fixture = tmp_path / "fixture.jsonl"
    fixture.write_text("", encoding="utf-8")
    cases = load_cases(case_file)
    with pytest.raises(ValueError, match="missing cases"):
        evaluate_fixture(cases, fixture)


def _fixture_case(**updates: object) -> RagEvalCase:
    value: dict[str, object] = {
        "case_id": "rag-fixture-negative",
        "corpus": "POLICY",
        "query": "订单预览",
        "expected_document_ids": ["policy-order-inventory"],
        "expected_sections": ["订单预览"],
    }
    value.update(updates)
    return RagEvalCase.model_validate(value)


def _write_fixture_row(path: Path, *rows: dict[str, object]) -> None:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def test_rag_fixture_rejects_legacy_document_id_score_arrays(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.jsonl"
    _write_fixture_row(fixture, {
        "case_id": "rag-fixture-negative",
        "result_ids": ["policy-order-inventory"],
        "scores": [.9],
    })
    with pytest.raises(ValueError, match="legacy result_ids/scores"):
        evaluate_fixture([_fixture_case()], fixture)


def test_rag_fixture_rejects_missing_chunk_contract_field(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.jsonl"
    _write_fixture_row(fixture, {
        "case_id": "rag-fixture-negative",
        "results": [{"document_id": "policy-order-inventory", "section_path": "订单预览", "score": .9}],
    })
    with pytest.raises(ValueError, match="catalog_ref"):
        evaluate_fixture([_fixture_case()], fixture)


def test_rag_fixture_rejects_wrong_section_path(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.jsonl"
    _write_fixture_row(fixture, {
        "case_id": "rag-fixture-negative",
        "results": [{"document_id": "policy-order-inventory", "section_path": "订单确认", "catalog_ref": None, "score": .9}],
    })
    with pytest.raises(ValueError, match="unexpected section_path"):
        evaluate_fixture([_fixture_case()], fixture)


def test_rag_fixture_rejects_forbidden_catalog_ref(tmp_path: Path) -> None:
    case = _fixture_case(
        case_id="rag-book-forbidden",
        corpus="BOOK_GUIDE",
        query="分布式",
        expected_document_ids=["book-guide-b36"],
        expected_sections=["核心主题"],
        forbidden_catalog_refs=["B36"],
    )
    fixture = tmp_path / "fixture.jsonl"
    _write_fixture_row(fixture, {
        "case_id": case.case_id,
        "results": [{"document_id": "book-guide-b36", "section_path": "核心主题", "catalog_ref": "B36", "score": .9}],
    })
    with pytest.raises(ValueError, match="hard filters"):
        evaluate_fixture([case], fixture)


def test_rag_fixture_rejects_result_that_ignores_catalog_filter(tmp_path: Path) -> None:
    case = _fixture_case(
        case_id="rag-book-filter",
        corpus="BOOK_GUIDE",
        query="分布式",
        expected_document_ids=["book-guide-b36"],
        expected_sections=["核心主题"],
        allowed_catalog_refs=["B36"],
        catalog_filters={"language": "zh-CN"},
    )
    fixture = tmp_path / "fixture.jsonl"
    _write_fixture_row(fixture, {
        "case_id": case.case_id,
        "results": [{"document_id": "book-guide-b36", "section_path": "核心主题", "catalog_ref": "B36", "score": .9}],
    })
    with pytest.raises(ValueError, match="hard filters"):
        evaluate_fixture([case], fixture)


def test_rag_fixture_rejects_extra_and_duplicate_cases(tmp_path: Path) -> None:
    row = {
        "case_id": "rag-fixture-negative",
        "results": [{"document_id": "policy-order-inventory", "section_path": "订单预览", "catalog_ref": None, "score": .9}],
    }
    extra = tmp_path / "extra.jsonl"
    _write_fixture_row(extra, row, {**row, "case_id": "not-in-cases"})
    with pytest.raises(ValueError, match="unknown case_id"):
        evaluate_fixture([_fixture_case()], extra)

    duplicate = tmp_path / "duplicate.jsonl"
    _write_fixture_row(duplicate, row, row)
    with pytest.raises(ValueError, match="duplicate RAG fixture case_id"):
        evaluate_fixture([_fixture_case()], duplicate)


@pytest.mark.asyncio
async def test_existing_manifest_rejects_incompatible_space_but_empty_workspace_can_create_fixture(tmp_path: Path) -> None:
    from app.models.embeddings import EmbeddingResponse

    (tmp_path / "policies").mkdir()
    (tmp_path / "books").mkdir()
    (tmp_path / "policies" / "policy.md").write_text(
        "---\n"
        "document_id: policy-space\n"
        "document_type: POLICY\n"
        "title: Space policy\n"
        "version: '1'\n"
        "effective_date: '2026-09-25'\n"
        "locale: zh-CN\n"
        "source_type: CURATED_INTERNAL\n"
        "source_uri: policy.md\n"
        "---\n\n## Rules\nUse the current space.\n",
        encoding="utf-8",
    )
    (tmp_path / "books" / "book.md").write_text(
        "---\n"
        "document_id: book-guide-b31\n"
        "document_type: BOOK_GUIDE\n"
        "catalog_ref: B31\n"
        "isbn: '9780000010315'\n"
        "title: Space book\n"
        "version: '1'\n"
        "effective_date: '2026-09-25'\n"
        "locale: zh-CN\n"
        "source_type: CURATED_SYNTHETIC\n"
        "source_uri: book.md\n"
        "---\n\n## Rules\nUse the current space.\n",
        encoding="utf-8",
    )
    loader = RagDocumentLoader(target_tokens=256, overlap_tokens=32)
    policy_chunk = loader.chunks_for(loader.load_file(tmp_path / "policies" / "policy.md"))[0]
    manifest = RagManifest.build(
        build_id="retained-space",
        embedding_model="old-model",
        embedding_dimensions=3,
        target_tokens=256,
        overlap_tokens=32,
        policy_threshold=.2,
        book_threshold=.2,
        policy_alias="policy-current",
        book_alias="book-current",
        chunks=[policy_chunk],
    )
    manifest_path = tmp_path / "manifest.json"
    manifest.write(manifest_path)

    class Provider(EmbeddingProvider):
        async def embed(self, texts: list[str], model: str) -> EmbeddingResponse:
            return EmbeddingResponse(vectors=[[.1, .2] for _ in texts])

    class Store:
        def __init__(self, alias: str) -> None:
            self.alias = alias

    with pytest.raises(ValueError, match="incompatible"):
        await RAGIndexer(
            loader,
            Provider(),
            embedding_model="fixture-embedding",
            dimensions=2,
            policy_store=Store("policy-current"),  # type: ignore[arg-type]
            book_store=Store("book-current"),  # type: ignore[arg-type]
        ).build(
            tmp_path,
            resolve_book=lambda isbn: None,  # type: ignore[arg-type]
            policy_threshold=.2,
            book_threshold=.2,
            manifest_path=manifest_path,
        )


def test_first_index_requires_explicit_chunk_pair_and_thresholds() -> None:
    args = SimpleNamespace(
        chunk_target=None,
        chunk_overlap=None,
        policy_threshold=None,
        book_threshold=None,
        fixture_parameters=False,
        tuning_report=None,
        corpus="all",
    )
    with pytest.raises(ValueError, match="first non-test RAG build requires"):
        _resolve_build_options(args, None, SimpleNamespace(environment="development"), None)  # type: ignore[arg-type]


def test_first_non_test_index_with_explicit_parameters_still_requires_real_tuning_report() -> None:
    args = SimpleNamespace(
        chunk_target=256,
        chunk_overlap=32,
        policy_threshold=.61,
        book_threshold=.58,
        fixture_parameters=False,
        tuning_report=None,
        corpus="all",
    )
    with pytest.raises(ValueError, match="first non-test RAG build requires"):
        _resolve_build_options(args, None, SimpleNamespace(environment="development"), None)  # type: ignore[arg-type]


def test_test_fixture_parameters_authorize_only_explicit_provider_free_options() -> None:
    args = SimpleNamespace(
        chunk_target=256,
        chunk_overlap=32,
        policy_threshold=.61,
        book_threshold=.58,
        fixture_parameters=True,
        tuning_report=None,
        corpus="all",
    )
    assert _resolve_build_options(args, None, SimpleNamespace(environment="test"), None) == (256, 32, .61, .58)  # type: ignore[arg-type]


def test_existing_manifest_inherits_every_index_parameter_when_omitted() -> None:
    previous = RagManifest.build(
        build_id="retained",
        embedding_model="fixture-embedding",
        embedding_dimensions=32,
        target_tokens=384,
        overlap_tokens=48,
        policy_threshold=.61,
        book_threshold=.58,
        policy_alias="policy-current",
        book_alias="book-current",
        chunks=[],
    )
    args = SimpleNamespace(
        chunk_target=None,
        chunk_overlap=None,
        policy_threshold=None,
        book_threshold=None,
        fixture_parameters=False,
        tuning_report=None,
        corpus="books",
    )
    assert _resolve_build_options(args, previous, SimpleNamespace(environment="development"), None) == (384, 48, .61, .58)  # type: ignore[arg-type]


def test_partial_index_rejects_explicit_parameter_change() -> None:
    previous = RagManifest.build(
        build_id="retained",
        embedding_model="fixture-embedding",
        embedding_dimensions=32,
        target_tokens=384,
        overlap_tokens=48,
        policy_threshold=.61,
        book_threshold=.58,
        policy_alias="policy-current",
        book_alias="book-current",
        chunks=[],
    )
    args = SimpleNamespace(
        chunk_target=256,
        chunk_overlap=32,
        policy_threshold=.61,
        book_threshold=.58,
        fixture_parameters=False,
        tuning_report=None,
        corpus="books",
    )
    with pytest.raises(ValueError, match="complete --corpus all"):
        _resolve_build_options(args, previous, SimpleNamespace(environment="development"), None)  # type: ignore[arg-type]


def test_fixture_tuning_report_cannot_authorize_an_index(tmp_path: Path) -> None:
    report = {
        "schema_version": 1,
        "mode": "FIXTURE",
        "evaluation_layer": "retrieval_and_filtering",
        "split": "TUNING",
        "created_at": "2026-09-25T00:00:00Z",
        "embedding": {"model": "fixture-embedding", "dimensions": 32, "distance": "Cosine"},
        "dataset_hash": "0" * 64,
        "source_tree_hash": "0" * 64,
        "selected_chunking": {"tokenizer": "cl100k_base", "target_tokens": 256, "overlap_tokens": 32},
        "thresholds": {"POLICY": .6, "BOOK_GUIDE": .6},
        "metrics": {},
        "provider_calls": 0,
        "report_hash": "0" * 64,
    }
    path = tmp_path / "fixture-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    settings = SimpleNamespace(embedding_model="fixture-embedding", embedding_dimensions=32)
    with pytest.raises(ValueError, match="REAL_PROVIDER"):
        _read_tuning_report(path, settings, Path(__file__).parents[2], Path("evals/rag-v2/tuning.jsonl"))  # type: ignore[arg-type]


def test_tuning_dataset_must_be_retained_under_agent_evals(tmp_path: Path) -> None:
    repo_root = Path(__file__).parents[2]
    assert _resolve_tuning_dataset(repo_root, Path("evals/rag-v3/tuning.jsonl")).name == "tuning.jsonl"
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="below agent/evals"):
        _resolve_tuning_dataset(repo_root, outside)


def test_rag_command_settings_do_not_require_chat_redis_mysql_or_jwt() -> None:
    settings = RagCommandSettings(
        _env_file=None,
        embedding_base_url="https://embedding.example/v1",
        embedding_api_key="synthetic-embedding-test-key",
        qdrant_url="http://qdrant:6333",
        environment="test",
    )
    assert settings.embedding_model == "qwen3.7-text-embedding"
    for field in ("model_api_key", "redis_addr", "agent_mysql_dsn", "jwt_public_key_path"):
        assert field not in RagCommandSettings.model_fields
        assert not hasattr(settings, field)


def test_rag_evaluation_parser_accepts_only_all_baseline() -> None:
    parsed = _parser().parse_args(["run", "--split", "tuning"])
    assert parsed.baseline == "all"
    with pytest.raises(SystemExit):
        _parser().parse_args(["run", "--split", "tuning", "--baseline", "rag"])


@pytest.mark.asyncio
async def test_live_evaluation_rejects_single_baseline_before_provider() -> None:
    args = SimpleNamespace(baseline="keyword")
    with pytest.raises(ValueError, match="only accepts all"):
        await _live_evaluate(args, [])


@pytest.mark.asyncio
async def test_first_index_missing_options_fails_before_embedding_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import app.rag_index as rag_index

    settings = SimpleNamespace(
        environment="development",
        embedding_model="qwen3.7-text-embedding",
        embedding_dimensions=1024,
    )
    calls: list[object] = []
    monkeypatch.setattr(rag_index, "RagCommandSettings", lambda: settings)
    monkeypatch.setattr(rag_index, "OpenAICompatibleEmbeddingProvider", lambda *args, **kwargs: calls.append(args))
    args = SimpleNamespace(
        allow_provider_calls=True,
        corpus="all",
        manifest=str(tmp_path / "missing-manifest.json"),
        tuning_report=None,
        fixture_parameters=False,
        chunk_target=None,
        chunk_overlap=None,
        policy_threshold=None,
        book_threshold=None,
        build_id=None,
    )
    with pytest.raises(ValueError, match="first non-test RAG build requires"):
        await rag_index._build(args)
    assert calls == []

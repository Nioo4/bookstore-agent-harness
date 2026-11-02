from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.embeddings import EmbeddingResponse, EmbeddingProvider
from app.rag.contracts import CatalogBook, RagChunk, RagDocumentLoader, RagManifest, source_tree_hashes
from app.rag.evaluation import RetrievalObservation, select_threshold
from app.rag.indexer import RAGIndexer
from app.rag.service import RagSearchService
from app.rag_evaluation import evaluate_fixture, validate_split


def frontmatter(*, document_type: str = "POLICY", extra: str = "") -> str:
    identity = "" if document_type == "POLICY" else "catalog_ref: B31\nisbn: '9780000010315'\n"
    source = "CURATED_INTERNAL" if document_type == "POLICY" else "CURATED_SYNTHETIC"
    return (
        "---\n"
        f"document_id: {'policy-test' if document_type == 'POLICY' else 'book-guide-b31'}\n"
        f"document_type: {document_type}\n"
        f"{identity}"
        "title: Test Book\nversion: '1.0'\neffective_date: '2026-09-25'\nlocale: zh-CN\n"
        f"source_type: {source}\nsource_uri: test.md\n{extra}---\n\n## 适读人群\n适合入门读者。\n"
    )


def test_v2_loader_uses_catalog_identity_and_stable_point_without_book_id(tmp_path: Path) -> None:
    path = tmp_path / "book.md"
    path.write_text(frontmatter(document_type="BOOK_GUIDE"), encoding="utf-8")
    loader = RagDocumentLoader(target_tokens=256, overlap_tokens=32)
    document = loader.load_file(path)
    chunks_a = loader.chunks_for(document, book_id=31)
    chunks_b = loader.chunks_for(document, book_id=999)
    assert chunks_a[0].point_id == chunks_b[0].point_id
    assert chunks_a[0].content_hash == chunks_b[0].content_hash
    assert chunks_a[0].book_id == 31
    assert chunks_a[0].payload()["document_type"] == "BOOK_GUIDE"


def test_source_tree_hash_covers_filter_identity_but_not_environment_book_id(tmp_path: Path) -> None:
    loader = RagDocumentLoader(target_tokens=256, overlap_tokens=32)
    policy_path = tmp_path / "policy.md"
    policy_path.write_text(frontmatter(), encoding="utf-8")
    book_path = tmp_path / "book.md"
    book_path.write_text(frontmatter(document_type="BOOK_GUIDE"), encoding="utf-8")
    policy_chunk = loader.chunks_for(loader.load_file(policy_path))[0]
    book_chunk = loader.chunks_for(loader.load_file(book_path), book_id=31)[0]

    policy_hash, book_hash, top_hash = source_tree_hashes([policy_chunk, book_chunk])
    assert source_tree_hashes([policy_chunk, book_chunk.model_copy(update={"book_id": 999})]) == (
        policy_hash,
        book_hash,
        top_hash,
    )

    for field, value in {
        "locale": "en-US",
        "source_type": "CURATED_INTERNAL",
        "catalog_ref": "B32",
        "isbn": "9780000010322",
    }.items():
        changed = book_chunk.model_copy(update={field: value})
        changed_policy_hash, changed_book_hash, changed_top_hash = source_tree_hashes([policy_chunk, changed])
        assert changed_policy_hash == policy_hash
        assert changed_book_hash != book_hash
        assert changed_top_hash != top_hash


def test_v2_loader_rejects_policy_catalog_identity_and_old_frontmatter(tmp_path: Path) -> None:
    invalid = tmp_path / "policy.md"
    invalid.write_text(frontmatter(extra="catalog_ref: B31\n"), encoding="utf-8")
    with pytest.raises(ValueError, match="POLICY documents cannot"):
        RagDocumentLoader().load_file(invalid)

    old = tmp_path / "old.md"
    old.write_text("---\ntitle: Old\nversion: '1'\neffective_date: '2026-09-25'\ndocument_type: guide\nlocale: zh-CN\n---\n\n## Rules\ntext", encoding="utf-8")
    with pytest.raises(ValueError, match="document_id"):
        RagDocumentLoader().load_file(old)


@pytest.mark.asyncio
async def test_book_guide_resolves_isbn_and_catalog_ref_without_trusting_markdown_book_id() -> None:
    class Resolver:
        async def get_by_isbn(self, isbn: str):
            return CatalogBook(book_id=73, isbn=isbn, title="Test Book", status="ACTIVE")

    loader = RagDocumentLoader(target_tokens=256, overlap_tokens=32)
    path = Path(__file__).parent / "_temporary_book_guide_fixture.md"
    path.write_text(frontmatter(document_type="BOOK_GUIDE"), encoding="utf-8")
    try:
        document = loader.load_file(path)
        result = await loader.validate_book_guides([document], Resolver(), catalog_refs={"B31": "9780000010315"})
        assert result[document.document_id].book_id == 73
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_book_guide_rejects_duplicate_isbn_even_when_document_ids_differ(tmp_path: Path) -> None:
    first = tmp_path / "b31.md"
    second = tmp_path / "b32.md"
    first.write_text(frontmatter(document_type="BOOK_GUIDE"), encoding="utf-8")
    second.write_text(
        frontmatter(document_type="BOOK_GUIDE")
        .replace("book-guide-b31", "book-guide-b32")
        .replace("catalog_ref: B31", "catalog_ref: B32"),
        encoding="utf-8",
    )

    class Resolver:
        async def get_by_isbn(self, isbn: str):
            return CatalogBook(book_id=73, isbn=isbn, title="Test Book", status="ACTIVE")

    loader = RagDocumentLoader(target_tokens=256, overlap_tokens=32)
    documents = [loader.load_file(first), loader.load_file(second)]
    with pytest.raises(ValueError, match="duplicate BOOK_GUIDE isbn"):
        await loader.validate_book_guides(
            documents,
            Resolver(),
            catalog_refs={"B31": "9780000010315", "B32": "9780000010315"},
        )


@pytest.mark.asyncio
async def test_blank_book_query_does_not_call_embedding_provider() -> None:
    class Provider(EmbeddingProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def embed(self, texts: list[str], model: str) -> EmbeddingResponse:
            self.calls += 1
            raise AssertionError("blank query must not reach the embedding provider")

    provider = Provider()
    service = RagSearchService(
        provider,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        embedding_model="qwen3.7-text-embedding",
        policy_threshold=0.2,
        book_threshold=0.2,
    )
    assert await service.search_book_knowledge("  ", [73]) == []
    assert provider.calls == 0


def test_manifest_is_nested_and_self_hashes() -> None:
    loader = RagDocumentLoader(target_tokens=256, overlap_tokens=32)
    # A policy document can be built without a Catalog call.
    from datetime import date
    from app.rag.contracts import RagDocument

    document = RagDocument.model_validate(
        {
            "document_id": "policy-test",
            "document_type": "POLICY",
            "title": "Test",
            "version": "1",
            "effective_date": date(2026, 9, 25),
            "locale": "zh-CN",
            "source_type": "CURATED_INTERNAL",
            "source_uri": "test",
            "body": "## Rules\ntext",
        }
    )
    chunk = loader.chunks_for(document)[0]
    manifest = RagManifest.build(
        build_id="build-1",
        embedding_model="qwen3.7-text-embedding",
        embedding_dimensions=1024,
        target_tokens=256,
        overlap_tokens=32,
        policy_threshold=.7,
        book_threshold=.8,
        policy_alias="bookstore_policy_current",
        book_alias="bookstore_book_knowledge_current",
        chunks=[chunk],
    )
    manifest.verify_hash()
    assert manifest.schema_version == 1
    assert manifest.chunking.tokenizer == "cl100k_base"
    assert manifest.corpora["POLICY"].chunks == 1


def test_threshold_selection_obeys_recall_and_abstention_gate() -> None:
    selection = select_threshold(
        [
            RetrievalObservation(("d1", "d2"), (0.9, 0.1), frozenset({"d1"})),
            RetrievalObservation(("d2",), (0.8,), frozenset(), True),
        ]
    )
    assert selection.passed is True
    assert selection.threshold == .9


def test_manual_dataset_has_exact_active_book_guide_coverage_and_eval_splits() -> None:
    project_root = Path(__file__).parents[2]
    dataset = json.loads((project_root / "deploy" / "datasets" / "manual-v1.json").read_text(encoding="utf-8"))
    active = {
        str(book["alias"]): book
        for book in dataset["books"]
        if book.get("status") == "ACTIVE"
    }
    documents = RagDocumentLoader().load_corpus(project_root / "knowledge", "books")
    assert len(documents) == len(active) == 98
    assert len({document.catalog_ref for document in documents}) == len(documents)
    assert {document.catalog_ref for document in documents} == set(active)
    for document in documents:
        book = active[document.catalog_ref]
        assert document.isbn == book["isbn"]
        assert document.title == book["title"]
        assert len(document.body) >= 350

    core_sections = [dict(RagDocumentLoader._sections(document.body))["核心主题"] for document in documents]
    exercise_sections = [dict(RagDocumentLoader._sections(document.body))["可验证练习"] for document in documents]
    assert "书目元数据与检索边界" not in "\n".join(core_sections)
    assert len(set(core_sections)) >= 90
    assert len(set(exercise_sections)) >= 90

    tuning = validate_split(project_root / "agent" / "evals" / "rag-v2" / "tuning.jsonl", expected_count=24)
    holdout = validate_split(project_root / "agent" / "evals" / "rag-v2" / "holdout.jsonl", expected_count=56)
    assert len({case.case_id for case in tuning + holdout}) == 80
    fixture = evaluate_fixture(tuning, project_root / "agent" / "evals" / "rag-v2" / "fixtures" / "tuning.jsonl")
    assert fixture["mode"] == "FIXTURE"
    assert fixture["evaluation_layer"] == "retrieval_and_filtering"
    assert fixture["answer_quality_status"] == "NOT_RUN"
    assert fixture["provider_calls"] == 0


class _EmbeddingFixture(EmbeddingProvider):
    async def embed(self, texts: list[str], model: str) -> EmbeddingResponse:
        return EmbeddingResponse(vectors=[[0.1, 0.2] for _ in texts])


class _IndexStore:
    def __init__(self, alias: str) -> None:
        self.alias = alias
        self.swaps: list[str] = []
        self.multi_swaps: list[list[tuple[str, str]]] = []
        self.smokes: list[str] = []
        self.payloads: dict[str, dict[str, dict[str, object]]] = {}

    async def create_physical_collection(self, collection: str) -> None:
        return None

    async def create_payload_indexes(self, collection: str, fields: tuple[str, ...]) -> None:
        return None

    async def upsert(self, collection: str, chunks: list[RagChunk], vectors: list[list[float]]) -> int:
        self.payloads.setdefault(collection, {}).update({chunk.point_id: chunk.payload() for chunk in chunks})
        return len(chunks)

    async def scroll_payloads(self, collection: str) -> dict[str, dict[str, object]]:
        return self.payloads.get(collection, {})

    async def smoke_query(self, collection: str, vector: list[float], **kwargs: object) -> None:
        self.smokes.append(collection)

    async def assert_alias_compatible(self, collection: str) -> None:
        return None

    async def swap_alias(self, collection: str) -> None:
        self.swaps.append(collection)

    async def swap_aliases(self, mappings: list[tuple[str, str]]) -> None:
        self.multi_swaps.append(mappings)


class _FailingSwapStore(_IndexStore):
    async def swap_alias(self, collection: str) -> None:
        raise RuntimeError("injected alias switch failure")


def _write_index_fixture(root: Path) -> tuple[Path, Path]:
    policies = root / "policies"
    books = root / "books"
    policies.mkdir()
    books.mkdir()
    (policies / "policy.md").write_text(frontmatter(), encoding="utf-8")
    (books / "book.md").write_text(frontmatter(document_type="BOOK_GUIDE"), encoding="utf-8")
    return policies, books


@pytest.mark.asyncio
async def test_indexer_all_switches_both_aliases_in_one_request(tmp_path: Path) -> None:
    _write_index_fixture(tmp_path)
    policy_store = _IndexStore("policy-current")
    book_store = _IndexStore("book-current")
    indexer = RAGIndexer(
        RagDocumentLoader(target_tokens=256, overlap_tokens=32),
        _EmbeddingFixture(),
        embedding_model="qwen3.7-text-embedding",
        dimensions=2,
        policy_store=policy_store,  # type: ignore[arg-type]
        book_store=book_store,  # type: ignore[arg-type]
    )

    async def resolve(isbn: str):
        return {"book_id": 73, "isbn": isbn, "title": "Test Book", "status": "ACTIVE"}

    result = await indexer.build(
        tmp_path,
        corpus="all",
        resolve_book=resolve,
        policy_threshold=0.2,
        book_threshold=0.2,
    )
    assert len(policy_store.multi_swaps) == 1
    assert policy_store.multi_swaps[0] == [
        ("policy-current", result.policy_collection),
        ("book-current", result.book_collection),
    ]
    assert not policy_store.swaps and not book_store.swaps
    assert policy_store.smokes and book_store.smokes


def _previous_two_corpus_manifest(root: Path) -> RagManifest:
    loader = RagDocumentLoader(target_tokens=256, overlap_tokens=32)
    policy = loader.chunks_for(loader.load_file(root / "policies" / "policy.md"))[0]
    book = loader.chunks_for(loader.load_file(root / "books" / "book.md"), book_id=73)[0]
    return RagManifest.build(
        build_id="old-build",
        embedding_model="qwen3.7-text-embedding",
        embedding_dimensions=2,
        target_tokens=256,
        overlap_tokens=32,
        policy_threshold=.2,
        book_threshold=.2,
        policy_alias="policy-current",
        book_alias="book-current",
        policy_collection="policy-old-collection",
        book_collection="book-old-collection",
        chunks=[policy, book],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("corpus,target", [("policies", "policy"), ("books", "book")])
async def test_partial_build_switches_only_target_and_inherits_untouched_manifest(
    tmp_path: Path, corpus: str, target: str
) -> None:
    _write_index_fixture(tmp_path)
    previous = _previous_two_corpus_manifest(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    previous.write(manifest_path)
    original_manifest = manifest_path.read_bytes()
    policy_store = _IndexStore("policy-current")
    book_store = _IndexStore("book-current")
    indexer = RAGIndexer(
        RagDocumentLoader(target_tokens=256, overlap_tokens=32),
        _EmbeddingFixture(),
        embedding_model="qwen3.7-text-embedding",
        dimensions=2,
        policy_store=policy_store,  # type: ignore[arg-type]
        book_store=book_store,  # type: ignore[arg-type]
    )

    async def resolve(isbn: str):
        return {"book_id": 73, "isbn": isbn, "title": "Test Book", "status": "ACTIVE"}

    result = await indexer.build(
        tmp_path,
        corpus=corpus,
        resolve_book=resolve,
        policy_threshold=.2,
        book_threshold=.2,
        manifest_path=manifest_path,
        build_id=f"partial-{corpus}",
    )
    target_store = policy_store if target == "policy" else book_store
    untouched_store = book_store if target == "policy" else policy_store
    assert len(target_store.swaps) == 1
    assert untouched_store.swaps == []
    untouched_type = "BOOK_GUIDE" if target == "policy" else "POLICY"
    assert result.manifest.corpora[untouched_type].model_dump() == previous.corpora[untouched_type].model_dump()
    # The indexer only returns a fully prepared manifest; the caller persists
    # it after the alias switch succeeds.  Before that point the old file is
    # still the serving manifest.
    assert manifest_path.read_bytes() == original_manifest
    result.manifest.write(manifest_path)
    persisted = RagManifest.read(manifest_path)
    assert persisted.corpora[untouched_type].model_dump() == previous.corpora[untouched_type].model_dump()
    assert persisted.corpora["POLICY" if target == "policy" else "BOOK_GUIDE"].collection != previous.corpora["POLICY" if target == "policy" else "BOOK_GUIDE"].collection


@pytest.mark.asyncio
async def test_partial_alias_switch_failure_keeps_old_manifest_untouched(tmp_path: Path) -> None:
    _write_index_fixture(tmp_path)
    previous = _previous_two_corpus_manifest(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    previous.write(manifest_path)
    original_manifest = manifest_path.read_bytes()
    policy_store = _FailingSwapStore("policy-current")
    book_store = _IndexStore("book-current")
    indexer = RAGIndexer(
        RagDocumentLoader(target_tokens=256, overlap_tokens=32),
        _EmbeddingFixture(),
        embedding_model="qwen3.7-text-embedding",
        dimensions=2,
        policy_store=policy_store,  # type: ignore[arg-type]
        book_store=book_store,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="alias switch failure"):
        await indexer.build(
            tmp_path,
            corpus="policies",
            resolve_book=lambda isbn: _resolve_test_book(isbn),  # type: ignore[arg-type]
            policy_threshold=.2,
            book_threshold=.2,
            manifest_path=manifest_path,
            build_id="partial-failing",
        )
    assert manifest_path.read_bytes() == original_manifest
    assert RagManifest.read(manifest_path).model_dump() == previous.model_dump()


async def _resolve_test_book(isbn: str) -> dict[str, object]:
    return {"book_id": 73, "isbn": isbn, "title": "Test Book", "status": "ACTIVE"}

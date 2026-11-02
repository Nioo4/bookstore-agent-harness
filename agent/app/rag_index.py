"""Explicit v2 RAG index command.

Usage::

    python -m app.rag_index build --corpus all --allow-provider-calls

The flag is intentionally mandatory for a live build.  Importing this module
or starting the API never contacts an embedding provider.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import RagCommandSettings
from app.models.configuration import AGENT_ROOT
from app.models.configuration import duration_seconds
from app.models.embeddings import OpenAICompatibleEmbeddingProvider
from app.rag.contracts import RagDocumentLoader, RagManifest, source_tree_hashes
from app.rag.evaluation import RAG_EVALUATION_LAYER
from app.rag.indexer import RAGIndexer, _embedding_space_slug
from app.rag.qdrant import QdrantRagStore
from app.tools.clients import GrpcCatalogClient
from app.tools.schemas import GetBookInput


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and audit the versioned v2 RAG collections")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build new physical collections and atomically switch aliases")
    build.add_argument("--corpus", choices=("policies", "books", "all"), default="all")
    build.add_argument("--allow-provider-calls", action="store_true", help="explicitly authorize paid model calls")
    build.add_argument("--build-id", default=None)
    build.add_argument("--manifest", default=None)
    build.add_argument("--tuning-report", default=None)
    build.add_argument("--tuning-dataset", default="evals/rag-v2/tuning.jsonl")
    build.add_argument("--fixture-parameters", action="store_true", help="allow explicit provider-free test parameters")
    build.add_argument("--policy-threshold", type=float, default=None)
    build.add_argument("--book-threshold", type=float, default=None)
    # No defaults on the first build: the operator must explicitly record the
    # tuned pair and both thresholds.  With a compatible manifest these may
    # all be omitted and are inherited below.
    build.add_argument("--chunk-target", type=int, choices=(256, 384, 500), default=None)
    build.add_argument("--chunk-overlap", type=int, choices=(32, 48, 80), default=None)
    return parser


def _canonical_hash(payload: dict[str, object]) -> str:
    value = dict(payload)
    value.pop("report_hash", None)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _dataset_hash(path: Path) -> str:
    canonical = "\n".join(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) + "\n"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_tuning_dataset(repo_root: Path, value: Path) -> Path:
    eval_root = (repo_root / "agent" / "evals").resolve(strict=True)
    path = value if value.is_absolute() else repo_root / "agent" / value
    path = path.resolve(strict=True)
    if not path.is_relative_to(eval_root):
        raise ValueError("tuning dataset must be retained below agent/evals")
    return path


def _source_tree_hash(root: Path, loader: RagDocumentLoader) -> str:
    documents = loader.load_corpus(root / "knowledge", "all")
    chunks = []
    for document in documents:
        chunks.extend(loader.chunks_for(document, book_id=1 if document.document_type == "BOOK_GUIDE" else None))
    return source_tree_hashes(chunks)[2]


def _read_tuning_report(
    path: Path,
    settings: RagCommandSettings,
    repo_root: Path,
    tuning_dataset: Path,
) -> dict[str, object]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid tuning report: {exc}") from None
    if not isinstance(report, dict):
        raise ValueError("tuning report must be a JSON object")
    required = {
        "schema_version", "mode", "evaluation_layer", "split", "created_at", "embedding", "dataset_hash",
        "source_tree_hash", "selected_chunking", "thresholds", "metrics", "provider_calls", "report_hash",
    }
    if not required.issubset(report):
        raise ValueError("tuning report is missing required identity or metric fields")
    if report.get("schema_version") != 1 or report.get("mode") != "REAL_PROVIDER" or report.get("split") != "TUNING":
        raise ValueError("only a REAL_PROVIDER TUNING report can authorize indexing")
    if report.get("evaluation_layer") != RAG_EVALUATION_LAYER:
        raise ValueError("tuning report must identify the retrieval_and_filtering evaluation layer")
    if report.get("report_hash") != _canonical_hash(report):
        raise ValueError("tuning report hash mismatch")
    calls = report.get("provider_calls")
    if type(calls) is not int or calls <= 0:
        raise ValueError("tuning report must prove at least one provider call")
    embedding = report.get("embedding")
    if embedding != {"model": settings.embedding_model, "dimensions": settings.embedding_dimensions, "distance": "Cosine"}:
        raise ValueError("tuning report embedding space does not match runtime")
    chunking = report.get("selected_chunking")
    if not isinstance(chunking, dict) or chunking.get("tokenizer") != "cl100k_base":
        raise ValueError("tuning report selected_chunking is invalid")
    target, overlap = chunking.get("target_tokens"), chunking.get("overlap_tokens")
    if (target, overlap) not in {(256, 32), (384, 48), (500, 80)}:
        raise ValueError("tuning report uses an unsupported chunk configuration")
    thresholds = report.get("thresholds")
    if not isinstance(thresholds, dict) or set(thresholds) != {"POLICY", "BOOK_GUIDE"}:
        raise ValueError("tuning report must contain both corpus thresholds")
    if any(type(value) not in {int, float} or not 0 < float(value) <= 1 for value in thresholds.values()):
        raise ValueError("tuning report thresholds must be non-zero and at most 1")
    threshold_selection = report.get("threshold_selection")
    if not isinstance(threshold_selection, dict):
        raise ValueError("tuning report must include threshold gate results")
    for corpus in ("POLICY", "BOOK_GUIDE"):
        choice = threshold_selection.get(corpus)
        if not isinstance(choice, dict) or choice.get("passed") is not True:
            raise ValueError(f"tuning report {corpus} threshold gate did not pass")
        if (
            float(choice.get("recall_at_5", 0)) < .90
            or float(choice.get("section_recall", 0)) < .90
            or float(choice.get("false_positive_rate", 1)) > .05
        ):
            raise ValueError(f"tuning report {corpus} threshold gate is below the required metrics")
    dataset_path = _resolve_tuning_dataset(repo_root, tuning_dataset)
    if report.get("dataset_hash") != _dataset_hash(dataset_path):
        raise ValueError("tuning report dataset_hash does not match the current tuning split")
    loader = RagDocumentLoader(target_tokens=int(target), overlap_tokens=int(overlap))
    if report.get("source_tree_hash") != _source_tree_hash(repo_root, loader):
        raise ValueError("tuning report source_tree_hash does not match the current knowledge tree")
    return report


def _resolve_build_options(
    args: argparse.Namespace,
    previous: RagManifest | None,
    settings: RagCommandSettings,
    report: dict[str, object] | None,
) -> tuple[int, int, float, float]:
    chunk_explicit = args.chunk_target is not None or args.chunk_overlap is not None
    if chunk_explicit and (args.chunk_target is None or args.chunk_overlap is None):
        raise ValueError("chunk-target and chunk-overlap must be supplied together")
    threshold_explicit = args.policy_threshold is not None or args.book_threshold is not None
    if threshold_explicit and (args.policy_threshold is None or args.book_threshold is None):
        raise ValueError("policy-threshold and book-threshold must be supplied together")
    if args.fixture_parameters and settings.environment != "test":
        raise ValueError("--fixture-parameters is allowed only when ENVIRONMENT=test")
    if args.fixture_parameters and report is not None:
        raise ValueError("Fixture parameters cannot be combined with a tuning report")
    if args.fixture_parameters and (not chunk_explicit or not threshold_explicit):
        raise ValueError("--fixture-parameters requires an explicit chunk pair and both thresholds")
    report_chunk: tuple[int, int] | None = None
    report_thresholds: tuple[float, float] | None = None
    if report is not None:
        selected = report["selected_chunking"]
        thresholds = report["thresholds"]
        assert isinstance(selected, dict) and isinstance(thresholds, dict)
        report_chunk = (int(selected["target_tokens"]), int(selected["overlap_tokens"]))
        report_thresholds = (float(thresholds["POLICY"]), float(thresholds["BOOK_GUIDE"]))
        if chunk_explicit and (args.chunk_target, args.chunk_overlap) != report_chunk:
            raise ValueError("explicit chunk parameters conflict with the tuning report")
        if threshold_explicit and (args.policy_threshold, args.book_threshold) != report_thresholds:
            raise ValueError("explicit thresholds conflict with the tuning report")
    if previous is None:
        if report is None and not args.fixture_parameters:
            raise ValueError("first non-test RAG build requires --tuning-report")
        if report_chunk is not None:
            target, overlap = report_chunk
            policy_threshold, book_threshold = report_thresholds  # type: ignore[misc]
        else:
            target, overlap = args.chunk_target, args.chunk_overlap
            policy_threshold, book_threshold = args.policy_threshold, args.book_threshold
    else:
        target, overlap = (
            report_chunk if report_chunk is not None else
            ((args.chunk_target, args.chunk_overlap) if chunk_explicit else (previous.chunking.target_tokens, previous.chunking.overlap_tokens))
        )
        policy_threshold, book_threshold = (
            report_thresholds if report_thresholds is not None else
            ((args.policy_threshold, args.book_threshold) if threshold_explicit else (previous.thresholds["POLICY"], previous.thresholds["BOOK_GUIDE"]))
        )
        changed = (
            (target, overlap) != (previous.chunking.target_tokens, previous.chunking.overlap_tokens)
            or policy_threshold != previous.thresholds["POLICY"]
            or book_threshold != previous.thresholds["BOOK_GUIDE"]
        )
        if changed:
            if args.corpus != "all":
                raise ValueError("chunking or threshold changes require a complete --corpus all build")
            if report is None and not args.fixture_parameters:
                raise ValueError("parameter changes require a new REAL_PROVIDER tuning report")
    assert target is not None and overlap is not None
    assert policy_threshold is not None and book_threshold is not None
    if not 0 < policy_threshold <= 1 or not 0 < book_threshold <= 1:
        raise ValueError("RAG thresholds must be greater than 0 and at most 1")
    return target, overlap, policy_threshold, book_threshold


async def _build(args: argparse.Namespace) -> None:
    if not args.allow_provider_calls:
        raise SystemExit("refusing live embedding calls: add --allow-provider-calls explicitly")
    settings = RagCommandSettings()  # type: ignore[call-arg]
    policy_alias = os.getenv("POLICY_COLLECTION_ALIAS", "bookstore_policy_current")
    book_alias = os.getenv("BOOK_COLLECTION_ALIAS", "bookstore_book_knowledge_current")
    manifest_path = Path(args.manifest or os.getenv("RAG_INDEX_MANIFEST_PATH", "runtime/rag-index-manifest.json"))
    if not manifest_path.is_absolute():
        manifest_path = AGENT_ROOT / manifest_path
    previous_manifest = RagManifest.read(manifest_path) if manifest_path.exists() else None
    if previous_manifest is not None:
        previous_manifest.verify_hash()
        previous_manifest.assert_compatible(
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
        previous_manifest.assert_runtime_aliases(policy_alias=policy_alias, book_alias=book_alias)
    if previous_manifest is None and args.corpus != "all":
        raise ValueError("partial corpus build requires an existing manifest")
    tuning_report = None
    if args.tuning_report is not None:
        tuning_report_path = Path(args.tuning_report)
        if not tuning_report_path.is_absolute():
            tuning_report_path = AGENT_ROOT / tuning_report_path
        tuning_report = _read_tuning_report(
            tuning_report_path,
            settings,
            AGENT_ROOT.parent,
            Path(args.tuning_dataset),
        )
    chunk_target, chunk_overlap, policy_threshold, book_threshold = _resolve_build_options(
        args, previous_manifest, settings, tuning_report
    )
    # Indexing only needs the embedding provider.  Do not construct the full
    # ModelRuntime here: that runtime validates the API's retained production
    # vector-space registry, while an explicitly isolated test Fixture is
    # allowed to create a new empty space (for example, 32 dimensions).
    embeddings = OpenAICompatibleEmbeddingProvider(
        settings.embedding_base_url,
        settings.embedding_api_key.get_secret_value(),
        duration_seconds(settings.embedding_timeout),
        dimensions=settings.embedding_dimensions,
        batch_size=settings.embedding_batch_size,
    )
    policy = QdrantRagStore(
        settings.qdrant_url,
        alias=policy_alias,
        dimensions=settings.embedding_dimensions,
    )
    books = QdrantRagStore(
        settings.qdrant_url,
        alias=book_alias,
        dimensions=settings.embedding_dimensions,
    )
    # v2 uses one in-process Go business service; the stable container DNS is
    # exposed as BUSINESS_GRPC_ADDR.  Do not fall back to the removed
    # per-RPC addresses for an index build.
    catalog = GrpcCatalogClient(settings.business_grpc_addr)
    try:
        dataset_path = AGENT_ROOT.parent / "deploy" / "datasets" / "manual-v1.json"
        catalog_refs = {
            str(book["alias"]): str(book["isbn"])
            for book in json.loads(dataset_path.read_text(encoding="utf-8")).get("books", [])
            if isinstance(book, dict) and isinstance(book.get("alias"), str) and isinstance(book.get("isbn"), str)
        }
        active_catalog_refs = {
            str(book["alias"])
            for book in json.loads(dataset_path.read_text(encoding="utf-8")).get("books", [])
            if isinstance(book, dict) and book.get("status") == "ACTIVE" and isinstance(book.get("alias"), str)
        }
        async def resolve(isbn: str):
            return await catalog.get_book(GetBookInput(isbn=isbn), timeout=5)

        indexer = RAGIndexer(
            # The candidates are fixed by the Spec; tuning must be an explicit
            # separate run that writes a manifest before this command is used.
            loader=RagDocumentLoader(target_tokens=chunk_target, overlap_tokens=chunk_overlap),
            embeddings=embeddings,
            embedding_model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            policy_store=policy,
            book_store=books,
        )
        result = await indexer.build(
            AGENT_ROOT.parent / "knowledge",
            corpus=args.corpus,
            resolve_book=resolve,
            catalog_refs=catalog_refs,
            active_catalog_refs=active_catalog_refs,
            build_id=args.build_id or (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]),
            policy_threshold=policy_threshold,
            book_threshold=book_threshold,
            manifest_path=manifest_path,
        )
        result.manifest.write(manifest_path)
        # Retention runs only after both the alias transaction and manifest
        # replacement succeeded.  A failed build therefore cannot delete the
        # previously serving collection.
        space = _embedding_space_slug(settings.embedding_model)
        if args.corpus in {"all", "policies"}:
            await policy.retain_current_and_previous(
                f"bookstore_policy_{space[:48]}_{settings.embedding_dimensions}_",
                current=result.manifest.corpora["POLICY"].collection,
                previous=previous_manifest.corpora["POLICY"].collection if previous_manifest else None,
            )
        if args.corpus in {"all", "books"}:
            await books.retain_current_and_previous(
                f"bookstore_books_{space[:48]}_{settings.embedding_dimensions}_",
                current=result.manifest.corpora["BOOK_GUIDE"].collection,
                previous=previous_manifest.corpora["BOOK_GUIDE"].collection if previous_manifest else None,
            )
        print(
            f"RAG index switched: build_id={result.manifest.build_id} "
            f"policy_chunks={len(result.policy_chunks)} book_chunks={len(result.book_chunks)} "
            f"manifest={manifest_path}"
        )
    finally:
        await catalog.aclose()
        await embeddings.aclose()
        await policy.aclose()
        await books.aclose()


def main() -> None:
    args = _build_parser().parse_args()
    if args.command == "build":
        try:
            asyncio.run(_build(args))
        except SystemExit:
            raise
        except Exception as exc:
            raise SystemExit(f"RAG index build failed: {exc}") from None


if __name__ == "__main__":
    main()

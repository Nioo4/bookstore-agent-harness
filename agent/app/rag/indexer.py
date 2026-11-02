from __future__ import annotations

import uuid
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.models.embeddings import EmbeddingProvider
from app.rag.contracts import CatalogBook, RagChunk, RagDocument, RagDocumentLoader, RagManifest
from app.rag.qdrant import QdrantRagStore


@dataclass(frozen=True)
class IndexBuildResult:
    manifest: RagManifest
    policy_chunks: tuple[RagChunk, ...]
    book_chunks: tuple[RagChunk, ...]
    policy_collection: str
    book_collection: str


class RAGIndexer:
    """Build two physical collections, audit them, then atomically swap aliases."""

    def __init__(
        self,
        loader: RagDocumentLoader,
        embeddings: EmbeddingProvider,
        *,
        embedding_model: str,
        dimensions: int,
        policy_store: QdrantRagStore,
        book_store: QdrantRagStore,
    ) -> None:
        self.loader = loader
        self.embeddings = embeddings
        self.embedding_model = embedding_model
        self.dimensions = dimensions
        self.policy_store = policy_store
        self.book_store = book_store

    async def build(
        self,
        root: Path,
        *,
        corpus: str = "all",
        resolve_book: Callable[[str], Awaitable[Mapping[str, Any] | CatalogBook | None]] | None = None,
        catalog_refs: Mapping[str, str] | None = None,
        active_catalog_refs: Iterable[str] | None = None,
        build_id: str | None = None,
        policy_threshold: float | None = None,
        book_threshold: float | None = None,
        manifest_path: Path | None = None,
    ) -> IndexBuildResult:
        if corpus not in {"policies", "books", "all"}:
            raise ValueError("corpus must be policies, books, or all")
        documents = self.loader.load_corpus(root, corpus)  # no provider call yet
        if not documents:
            raise ValueError(f"{corpus} corpus is empty; refusing to create an empty manifest or alias")
        if corpus == "all":
            document_types = {document.document_type for document in documents}
            if document_types != {"POLICY", "BOOK_GUIDE"}:
                raise ValueError("all corpus build requires both POLICY and BOOK_GUIDE documents")
        previous: RagManifest | None = None
        if manifest_path is not None and manifest_path.exists():
            previous = RagManifest.read(manifest_path)
            previous.verify_hash()
            # A missing manifest denotes an independent/empty workspace and
            # may establish a new Fixture vector space.  Once a manifest is
            # retained, every corpus build must use its model/dimensions.
            previous.assert_compatible(model=self.embedding_model, dimensions=self.dimensions)
            if policy_threshold is None:
                policy_threshold = previous.thresholds["POLICY"]
            if book_threshold is None:
                book_threshold = previous.thresholds["BOOK_GUIDE"]
        if corpus != "all":
            if previous is None:
                raise ValueError("partial corpus build requires an existing manifest")
            if (
                policy_threshold != previous.thresholds["POLICY"]
                or book_threshold != previous.thresholds["BOOK_GUIDE"]
            ):
                raise ValueError("partial corpus build cannot change RAG thresholds")
            if (
                previous.chunking.target_tokens,
                previous.chunking.overlap_tokens,
            ) != (self.loader.target_tokens, self.loader.overlap_tokens):
                raise ValueError("partial corpus build must use the existing manifest chunk configuration")
            # A partial build may never silently move the untouched alias.
            untouched = previous.corpora["BOOK_GUIDE" if corpus == "policies" else "POLICY"]
            untouched_store = self.book_store if corpus == "policies" else self.policy_store
            if untouched.alias != untouched_store.alias:
                raise ValueError("partial build Alias does not match the current manifest")
            target_type = "POLICY" if corpus == "policies" else "BOOK_GUIDE"
            target_store = self.policy_store if corpus == "policies" else self.book_store
            if previous.corpora[target_type].alias != target_store.alias:
                raise ValueError("partial build target Alias does not match the current manifest")
            await untouched_store.assert_alias_compatible(untouched.collection)
        if policy_threshold is None or book_threshold is None:
            raise ValueError("RAG build requires tuned non-zero policy_threshold and book_threshold")
        if not 0 < policy_threshold <= 1 or not 0 < book_threshold <= 1:
            raise ValueError("RAG thresholds must be greater than 0 and at most 1")
        if any(document.document_type == "BOOK_GUIDE" for document in documents) and resolve_book is None:
            raise ValueError("BOOK_GUIDE indexing requires an authoritative Catalog resolver")
        resolved: dict[str, CatalogBook] = {}
        if resolve_book is not None:
            class _Resolver:
                async def get_by_isbn(self, isbn: str):
                    return await resolve_book(isbn)

            # Inactive/DRAFT cards may remain in the derived index for audit
            # and future reactivation.  Online Catalog filters decide whether
            # they are visible; the completeness gate below applies only to
            # ACTIVE catalog entries.
            resolved = await self.loader.validate_book_guides(
                documents,
                _Resolver(),
                require_active=False,
                catalog_refs=catalog_refs,
            )
        if active_catalog_refs is not None and corpus in {"books", "all"}:
            expected_active = {str(item) for item in active_catalog_refs}
            indexed_refs = {document.catalog_ref for document in documents if document.document_type == "BOOK_GUIDE"}
            if not expected_active.issubset(indexed_refs):
                missing = sorted(expected_active - indexed_refs)
                raise ValueError(f"ACTIVE Catalog coverage mismatch: missing={missing}")
        all_chunks: list[RagChunk] = []
        for document in documents:
            book_id = resolved[document.document_id].book_id if document.document_type == "BOOK_GUIDE" else None
            all_chunks.extend(self.loader.chunks_for(document, book_id=book_id))
        embedding_space = embedding_space_identity(self.embedding_model, self.dimensions)
        all_chunks = [chunk.with_embedding_space(embedding_space) for chunk in all_chunks]
        point_ids = [chunk.point_id for chunk in all_chunks]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError("duplicate point_id in RAG corpus; refusing to overwrite a source chunk")
        policy_chunks = tuple(chunk for chunk in all_chunks if chunk.document_type == "POLICY")
        book_chunks = tuple(chunk for chunk in all_chunks if chunk.document_type == "BOOK_GUIDE")
        if corpus == "policies":
            book_chunks = ()
        if corpus == "books":
            policy_chunks = ()
        build = build_id or uuid.uuid4().hex[:16]
        space = _embedding_space_slug(self.embedding_model)
        policy_collection = f"bookstore_policy_{space}_{self.dimensions}_{build}"
        book_collection = f"bookstore_books_{space}_{self.dimensions}_{build}"
        if policy_chunks:
            await self.policy_store.create_physical_collection(policy_collection)
            await self.policy_store.create_payload_indexes(policy_collection, ("document_id", "locale"))
            first_vector = await self._index_collection(self.policy_store, policy_collection, policy_chunks)
            await self._audit(self.policy_store, policy_collection, policy_chunks)
            await self.policy_store.smoke_query(
                policy_collection,
                first_vector,
                expected_point_ids={chunk.point_id for chunk in policy_chunks},
                expected_document_ids={chunk.document_id for chunk in policy_chunks},
            )
        if book_chunks:
            await self.book_store.create_physical_collection(book_collection)
            await self.book_store.create_payload_indexes(book_collection, ("book_id", "document_id", "locale"))
            first_vector = await self._index_collection(self.book_store, book_collection, book_chunks)
            await self._audit(self.book_store, book_collection, book_chunks)
            await self.book_store.smoke_query(
                book_collection,
                first_vector,
                expected_point_ids={chunk.point_id for chunk in book_chunks},
                expected_document_ids={chunk.document_id for chunk in book_chunks},
            )
        manifest = RagManifest.build(
            build_id=build,
            embedding_model=self.embedding_model,
            embedding_dimensions=self.dimensions,
            target_tokens=self.loader.target_tokens,
            overlap_tokens=self.loader.overlap_tokens,
            policy_threshold=policy_threshold,
            book_threshold=book_threshold,
            policy_alias=self.policy_store.alias,
            book_alias=self.book_store.alias,
            policy_collection=policy_collection,
            book_collection=book_collection,
            chunks=all_chunks,
        )
        if previous is not None and corpus != "all":
            # Preserve the untouched corpus metadata and physical collection;
            # its alias is not changed by this build.
            untouched_type = "BOOK_GUIDE" if corpus == "policies" else "POLICY"
            merged = manifest.model_copy(
                update={
                    "corpora": {**manifest.corpora, untouched_type: previous.corpora[untouched_type]},
                    "thresholds": {
                        **manifest.thresholds,
                        untouched_type: previous.thresholds[untouched_type],
                    },
                }
            )
            source = "\n".join(
                f"{kind}|{merged.corpora[kind].source_tree_hash}"
                for kind in ("POLICY", "BOOK_GUIDE")
            )
            canonical = merged.model_dump(mode="json")
            canonical["source_tree_hash"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
            canonical.pop("manifest_hash", None)
            digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            manifest = merged.model_copy(update={"source_tree_hash": canonical["source_tree_hash"], "manifest_hash": digest})
        # An alias is switched only after both collections have been uploaded
        # and smoke-tested.  The all-corpus switch is one Qdrant transaction.
        if corpus == "all":
            await self.policy_store.swap_aliases(
                [(self.policy_store.alias, policy_collection), (self.book_store.alias, book_collection)]
            )
        elif policy_chunks:
            await self.policy_store.swap_alias(policy_collection)
        elif book_chunks:
            await self.book_store.swap_alias(book_collection)
        return IndexBuildResult(
            manifest,
            policy_chunks,
            book_chunks,
            manifest.corpora["POLICY"].collection,
            manifest.corpora["BOOK_GUIDE"].collection,
        )

    async def _index_collection(self, store: QdrantRagStore, collection: str, chunks: Iterable[RagChunk]) -> list[float]:
        rows = list(chunks)
        first_vector: list[float] | None = None
        for offset in range(0, len(rows), 20):
            batch = rows[offset : offset + 20]
            response = await self.embeddings.embed([chunk.content for chunk in batch], self.embedding_model)
            if len(response.vectors) != len(batch):
                raise ValueError("embedding provider returned an invalid batch")
            if first_vector is None:
                first_vector = list(response.vectors[0])
            await store.upsert(collection, batch, response.vectors)
        if first_vector is None:
            raise ValueError("cannot index an empty collection")
        return first_vector

    async def _audit(self, store: QdrantRagStore, collection: str, chunks: Iterable[RagChunk]) -> None:
        expected = {chunk.point_id: chunk.payload() for chunk in chunks}
        actual = await store.scroll_payloads(collection)
        if actual != expected:
            raise ValueError("RAG collection audit failed; alias will not be switched")


def _embedding_space_slug(model: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", model).strip("_").lower()
    if not slug:
        raise ValueError("embedding model name cannot produce an empty collection slug")
    return slug[:48]


def embedding_space_identity(model: str, dimensions: int, distance: str = "Cosine") -> str:
    """Stable, credential-free identity stored in every Qdrant payload."""
    canonical = json.dumps(
        {"model": model, "dimensions": dimensions, "distance": distance},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

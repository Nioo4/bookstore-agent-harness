from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

import tiktoken
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


DocumentType = Literal["POLICY", "BOOK_GUIDE"]


def normalize_rag_content(value: str) -> str:
    """Normalize only transport whitespace used by the content hash."""
    return "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").splitlines()).strip()


def chunk_point_id(document_id: str, section_path: str, chunk_index: int, content_hash: str) -> str:
    seed = "|".join((document_id, section_path, str(chunk_index), content_hash))
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


class RagDocument(BaseModel):
    """Strict v2 front-matter plus normalized Markdown body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    document_type: DocumentType
    title: str = Field(min_length=1, max_length=255)
    version: str = Field(min_length=1, max_length=64)
    effective_date: date
    locale: str = Field(min_length=2, max_length=32)
    source_type: Literal["CURATED_INTERNAL", "CURATED_SYNTHETIC"]
    source_uri: str = Field(min_length=1, max_length=1024)
    catalog_ref: str | None = Field(default=None, pattern=r"^B[1-9][0-9]{0,3}$")
    isbn: str | None = Field(default=None, min_length=1, max_length=32)
    body: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_type_identity(self) -> "RagDocument":
        if self.document_type == "POLICY":
            if self.catalog_ref is not None or self.isbn is not None:
                raise ValueError("POLICY documents cannot contain catalog_ref or isbn")
            if self.source_type != "CURATED_INTERNAL":
                raise ValueError("POLICY documents must use CURATED_INTERNAL")
        else:
            if not self.catalog_ref or not self.isbn:
                raise ValueError("BOOK_GUIDE documents require catalog_ref and isbn")
            if self.source_type != "CURATED_SYNTHETIC":
                raise ValueError("BOOK_GUIDE documents must use CURATED_SYNTHETIC")
        return self


class RagChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    point_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    document_id: str
    document_type: DocumentType
    catalog_ref: str | None = None
    book_id: int | None = Field(default=None, gt=0)
    isbn: str | None = None
    document_title: str
    section_path: str
    chunk_index: int = Field(ge=0)
    version: str
    effective_date: date
    locale: str
    source_type: str
    source_uri: str
    content: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Filled by the indexer, never inferred by the online search path.  A
    # payload without this value cannot be audited as belonging to the
    # configured embedding space.
    embedding_space: str | None = Field(default=None, min_length=1)

    def payload(self) -> dict[str, Any]:
        """Return the exact payload contract used by both Qdrant collections."""
        return self.model_dump(mode="json", exclude_none=True)

    def with_embedding_space(self, identity: str) -> "RagChunk":
        if not identity.strip():
            raise ValueError("embedding_space identity must not be empty")
        return self.model_copy(update={"embedding_space": identity.strip()})


class CatalogBook(BaseModel):
    """The small, authoritative identity subset needed during indexing."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    book_id: int = Field(gt=0)
    catalog_ref: str | None = None
    isbn: str = Field(min_length=1)
    title: str = Field(min_length=1)
    status: str = Field(min_length=1)
    language_code: str | None = None
    categories: tuple[str, ...] = ()


class CatalogResolver(Protocol):
    async def get_by_isbn(self, isbn: str) -> Mapping[str, Any] | CatalogBook | None: ...


class RagDocumentLoader:
    """Load only the v2 corpus layout and produce stable, auditable chunks.

    ``cl100k_base`` is an engineering approximation for chunk sizing.  The
    embedding provider may use a different tokenizer; the manifest records the
    approximation so evaluation can compare it rather than silently assuming
    token identity.
    """

    def __init__(self, *, target_tokens: int = 500, overlap_tokens: int = 80) -> None:
        if (target_tokens, overlap_tokens) not in {(256, 32), (384, 48), (500, 80)}:
            raise ValueError("v2 chunk configuration must be one of 256/32, 384/48, 500/80")
        self.target_tokens = target_tokens
        self.overlap_tokens = overlap_tokens
        self.encoding = tiktoken.get_encoding("cl100k_base")

    def load_file(self, path: Path) -> RagDocument:
        raw = path.read_text(encoding="utf-8")
        metadata, body = self._frontmatter(raw)
        sections = self._sections(body)
        normalized_body = "\n\n".join(
            f"## {section}\n{text}" for section, text in sections
        ).strip()
        return RagDocument.model_validate({**metadata, "body": normalized_body})

    def load_corpus(self, root: Path, corpus: Literal["policies", "books", "all"]) -> list[RagDocument]:
        """Recursively load only ``knowledge/policies`` and ``knowledge/books``.

        The old root-level FAQ files are intentionally not included in v2.
        Keeping this boundary explicit prevents a forgotten legacy file from
        silently becoming an online source of evidence.
        """
        directories: list[Path]
        if corpus == "all":
            directories = [root / "policies", root / "books"]
        else:
            directories = [root / corpus]
        paths = sorted(path for directory in directories if directory.exists() for path in directory.rglob("*.md"))
        documents = [self.load_file(path) for path in paths]
        expected_type = {"policies": "POLICY", "books": "BOOK_GUIDE"}
        if corpus != "all" and any(document.document_type != expected_type[corpus] for document in documents):
            raise ValueError(f"{corpus} corpus contains a document of the wrong document_type")
        ids = [document.document_id for document in documents]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate document_id in RAG corpus")
        return documents

    def chunks_for(self, document: RagDocument, *, book_id: int | None = None) -> list[RagChunk]:
        if document.document_type == "BOOK_GUIDE" and book_id is None:
            raise ValueError("book_id must be resolved before chunking BOOK_GUIDE")
        chunks: list[RagChunk] = []
        for section_path, text in self._sections(document.body):
            section_content = f"## {section_path}\n{text}".strip()
            tokens = self.encoding.encode(section_content)
            step = self.target_tokens - self.overlap_tokens
            section_chunks: list[str] = []
            for start in range(0, len(tokens), step):
                token_slice = tokens[start : start + self.target_tokens]
                if not token_slice:
                    continue
                content = self._normalize(self.encoding.decode(token_slice))
                if content:
                    section_chunks.append(content)
                if start + self.target_tokens >= len(tokens):
                    break
            for index, content in enumerate(section_chunks):
                content_hash = _sha256(content)
                chunks.append(
                    RagChunk(
                        point_id=chunk_point_id(document.document_id, section_path, index, content_hash),
                        document_id=document.document_id,
                        document_type=document.document_type,
                        catalog_ref=document.catalog_ref,
                        book_id=book_id,
                        isbn=document.isbn,
                        document_title=document.title,
                        section_path=section_path,
                        chunk_index=index,
                        version=document.version,
                        effective_date=document.effective_date,
                        locale=document.locale,
                        source_type=document.source_type,
                        source_uri=document.source_uri,
                        content=content,
                        content_hash=content_hash,
                    )
                )
        if not chunks:
            raise ValueError(f"RAG document has no non-empty sections: {document.document_id}")
        point_ids = [chunk.point_id for chunk in chunks]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError(f"RAG document contains duplicate chunk point_id: {document.document_id}")
        return chunks

    @staticmethod
    async def validate_book_guides(
        documents: Iterable[RagDocument],
        resolver: CatalogResolver,
        *,
        require_active: bool = True,
        catalog_refs: Mapping[str, str] | None = None,
    ) -> dict[str, CatalogBook]:
        """Resolve ISBNs at build time; never trust environment-specific IDs in Markdown."""
        resolved: dict[str, CatalogBook] = {}
        seen_isbns: set[str] = set()
        for document in documents:
            if document.document_type != "BOOK_GUIDE":
                continue
            assert document.isbn is not None and document.catalog_ref is not None
            if catalog_refs is not None and catalog_refs.get(document.catalog_ref) != document.isbn:
                raise ValueError(f"BOOK_GUIDE catalog_ref/ISBN mismatch: {document.document_id}")
            if document.isbn in seen_isbns:
                raise ValueError(f"duplicate BOOK_GUIDE isbn: {document.isbn}")
            seen_isbns.add(document.isbn)
            raw = await resolver.get_by_isbn(document.isbn)
            if raw is None:
                raise ValueError(f"BOOK_GUIDE ISBN not found in Catalog: {document.isbn}")
            book = raw if isinstance(raw, CatalogBook) else CatalogBook.model_validate(raw)
            if book.isbn != document.isbn:
                raise ValueError(f"BOOK_GUIDE ISBN mismatch: {document.document_id}")
            if book.title != document.title:
                raise ValueError(f"BOOK_GUIDE title mismatch: {document.document_id}")
            if require_active and book.status != "ACTIVE":
                raise ValueError(f"BOOK_GUIDE points to non-ACTIVE book: {document.document_id}")
            resolved[document.document_id] = book
        return resolved

    @staticmethod
    def _frontmatter(raw: str) -> tuple[dict[str, Any], str]:
        match = re.match(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", raw, flags=re.DOTALL)
        if match is None:
            raise ValueError("RAG document must contain YAML frontmatter")
        metadata = yaml.safe_load(match.group(1))
        if not isinstance(metadata, dict):
            raise ValueError("RAG frontmatter must be a YAML object")
        return metadata, match.group(2)

    @staticmethod
    def _sections(body: str) -> list[tuple[str, str]]:
        sections: list[tuple[str, str]] = []
        current_heading: str | None = None
        current_lines: list[str] = []
        saw_h1 = False
        for line in body.splitlines():
            h2 = re.match(r"^##\s+(.+?)\s*$", line)
            if h2:
                if current_heading is not None and any(item.strip() for item in current_lines):
                    sections.append((current_heading, RagDocumentLoader._normalize("\n".join(current_lines))))
                current_heading = h2.group(1).strip()
                current_lines = []
                continue
            if current_heading is None:
                if not line.strip():
                    continue
                if not saw_h1 and re.match(r"^#(?!#)\s+\S", line):
                    saw_h1 = True
                    continue
                raise ValueError("RAG document contains body text before the first H2 section")
            current_lines.append(line)
        if current_heading is not None and any(item.strip() for item in current_lines):
            sections.append((current_heading, RagDocumentLoader._normalize("\n".join(current_lines))))
        if not sections:
            raise ValueError("RAG document must contain at least one non-empty H2 section")
        return sections

    @staticmethod
    def _normalize(value: str) -> str:
        return normalize_rag_content(value)


class RagEmbedding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    dimensions: int = Field(gt=0)
    distance: Literal["Cosine"] = "Cosine"


class RagChunking(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tokenizer: Literal["cl100k_base"] = "cl100k_base"
    target_tokens: int = Field(gt=0)
    overlap_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_overlap(self) -> "RagChunking":
        if self.overlap_tokens >= self.target_tokens:
            raise ValueError("chunk overlap must be smaller than target")
        return self


class RagCorpusManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alias: str = Field(min_length=1)
    collection: str = Field(min_length=1)
    documents: int = Field(ge=0)
    chunks: int = Field(ge=0)
    source_tree_hash: str = Field(default="0" * 64, pattern=r"^[0-9a-f]{64}$")


class RagManifest(BaseModel):
    """Version 1 of the v2 RAG index manifest.

    The nesting is deliberate: changing an embedding model, a chunking
    tokenizer, or either Alias is a compatibility change and must be visible
    in a diff rather than hidden in a flat string.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    build_id: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,80}$")
    created_at: datetime
    embedding: RagEmbedding
    chunking: RagChunking
    thresholds: dict[DocumentType, float]
    corpora: dict[DocumentType, RagCorpusManifest]
    source_tree_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_corpora(self) -> "RagManifest":
        if set(self.thresholds) != {"POLICY", "BOOK_GUIDE"}:
            raise ValueError("manifest thresholds must contain POLICY and BOOK_GUIDE")
        if set(self.corpora) != {"POLICY", "BOOK_GUIDE"}:
            raise ValueError("manifest corpora must contain POLICY and BOOK_GUIDE")
        # A zero threshold turns every vector into an answer candidate and is
        # therefore not an audited production setting.  A failed tuning gate
        # must be represented by a failed report, not by a zero manifest.
        if any(not 0 < value <= 1 for value in self.thresholds.values()):
            raise ValueError("RAG thresholds must be greater than 0 and at most 1")
        return self

    @classmethod
    def build(
        cls,
        *,
        build_id: str,
        embedding_model: str,
        embedding_dimensions: int,
        target_tokens: int,
        overlap_tokens: int,
        policy_threshold: float,
        book_threshold: float,
        policy_alias: str,
        book_alias: str,
        policy_collection: str | None = None,
        book_collection: str | None = None,
        chunks: Iterable[RagChunk],
    ) -> "RagManifest":
        all_chunks = list(chunks)
        policy_hash, book_hash, source_tree_hash = source_tree_hashes(all_chunks)
        manifest = cls(
            build_id=build_id,
            created_at=datetime.now(timezone.utc),
            embedding=RagEmbedding(model=embedding_model, dimensions=embedding_dimensions),
            chunking=RagChunking(target_tokens=target_tokens, overlap_tokens=overlap_tokens),
            thresholds={"POLICY": policy_threshold, "BOOK_GUIDE": book_threshold},
            corpora={
                "POLICY": RagCorpusManifest(
                    alias=policy_alias,
                    collection=policy_collection or policy_alias,
                    documents=len({chunk.document_id for chunk in all_chunks if chunk.document_type == "POLICY"}),
                    chunks=sum(chunk.document_type == "POLICY" for chunk in all_chunks),
                    source_tree_hash=policy_hash,
                ),
                "BOOK_GUIDE": RagCorpusManifest(
                    alias=book_alias,
                    collection=book_collection or book_alias,
                    documents=len({chunk.document_id for chunk in all_chunks if chunk.document_type == "BOOK_GUIDE"}),
                    chunks=sum(chunk.document_type == "BOOK_GUIDE" for chunk in all_chunks),
                    source_tree_hash=book_hash,
                ),
            },
            source_tree_hash=source_tree_hash,
            manifest_hash="0" * 64,
        )
        canonical = manifest.model_dump(mode="json")
        canonical.pop("manifest_hash", None)
        digest = _sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return manifest.model_copy(update={"manifest_hash": digest})

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            with temporary.open("r+b") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def read(cls, path: Path) -> "RagManifest":
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def assert_compatible(self, *, model: str, dimensions: int, distance: str = "Cosine") -> None:
        if (self.embedding.model, self.embedding.dimensions, self.embedding.distance) != (model, dimensions, distance):
            raise ValueError("RAG index manifest is incompatible with configured embedding space")

    def assert_runtime_aliases(self, *, policy_alias: str, book_alias: str) -> None:
        if self.corpora["POLICY"].alias != policy_alias or self.corpora["BOOK_GUIDE"].alias != book_alias:
            raise ValueError("RAG index manifest aliases do not match runtime configuration")

    def verify_hash(self) -> None:
        canonical = self.model_dump(mode="json")
        expected = canonical.pop("manifest_hash")
        actual = _sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        if expected != actual:
            raise ValueError("RAG index manifest hash mismatch")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_tree_hashes(chunks: Iterable[RagChunk]) -> tuple[str, str, str]:
    """Return the per-corpus and top-level hashes shared by reports/Manifest."""
    rows = list(chunks)

    def material(document_type: DocumentType) -> str:
        return "\n".join(
            f"{chunk.document_id}|{chunk.document_type}|{chunk.document_title}|{chunk.section_path}|"
            # These fields are part of the source contract even though they do
            # not change the point ID.  They affect locale/source trust and
            # Catalog filtering, so changing them must invalidate the source
            # tree hash and any tuning report derived from it.  ``book_id`` is
            # intentionally absent: it is an environment-specific Catalog
            # surrogate and is bound separately by the embedding Manifest.
            f"{chunk.chunk_index}|{chunk.version}|{chunk.effective_date.isoformat()}|{chunk.locale}|"
            f"{chunk.source_type}|{chunk.source_uri}|{chunk.catalog_ref or ''}|{chunk.isbn or ''}|{chunk.content_hash}"
            for chunk in sorted(
                (item for item in rows if item.document_type == document_type),
                key=lambda item: item.point_id,
            )
        )

    policy_hash = _sha256(material("POLICY"))
    book_hash = _sha256(material("BOOK_GUIDE"))
    return policy_hash, book_hash, _sha256(f"POLICY|{policy_hash}\nBOOK_GUIDE|{book_hash}")

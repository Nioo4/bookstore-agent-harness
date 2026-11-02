from __future__ import annotations

from collections.abc import Iterable
import hashlib
import re
from typing import Any

from app.rag.contracts import chunk_point_id, normalize_rag_content


def policy_evidence_id(content_hash: str) -> str:
    return f"policy:{content_hash}"


def book_evidence_id(book_id: int, content_hash: str) -> str:
    return f"book-guide:{book_id}:{content_hash}"


def _required_text(chunk: dict[str, Any], field: str) -> str:
    value = chunk.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"RAG result is missing {field}")
    return value


def validate_chunk_evidence(
    chunk: dict[str, Any],
    *,
    expected_type: str | None = None,
    expected_book_id: int | None = None,
    expected_source_uri: str | None = None,
    expected_section_path: str | None = None,
    expected_version: str | None = None,
) -> None:
    """Verify that a retrieved row is an actual indexed chunk.

    Qdrant payloads are untrusted input at the application boundary.  The
    evidence ID is not enough on its own: a malicious or stale payload could
    otherwise reuse a valid hash while changing its source metadata.  We
    require the complete chunk identity and recompute both the content hash
    and deterministic point ID from the returned fields.
    """
    document_type = chunk.get("document_type")
    if document_type not in {"POLICY", "BOOK_GUIDE"}:
        raise ValueError("RAG result has an invalid document_type")
    if expected_type is not None and document_type != expected_type:
        raise ValueError("RAG result has an unexpected document_type")
    document_id = _required_text(chunk, "document_id")
    document_title = _required_text(chunk, "document_title")
    section_path = _required_text(chunk, "section_path")
    version = _required_text(chunk, "version")
    _required_text(chunk, "effective_date")
    _required_text(chunk, "locale")
    source_type = _required_text(chunk, "source_type")
    source_uri = _required_text(chunk, "source_uri")
    content = _required_text(chunk, "content")
    content_hash = _required_text(chunk, "content_hash")
    if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        raise ValueError("RAG result has an invalid content_hash")
    if hashlib.sha256(normalize_rag_content(content).encode("utf-8")).hexdigest() != content_hash:
        raise ValueError("RAG result content_hash does not match content")
    chunk_index = chunk.get("chunk_index")
    if type(chunk_index) is not int or chunk_index < 0:
        raise ValueError("RAG result has an invalid chunk_index")
    point_id = chunk.get("point_id")
    if not isinstance(point_id, str) or not re.fullmatch(r"[0-9a-f]{32}", point_id):
        raise ValueError("RAG result is missing a valid point_id")
    if point_id != chunk_point_id(document_id, section_path, chunk_index, content_hash):
        raise ValueError("RAG result point_id does not match chunk metadata")
    embedding_space = chunk.get("embedding_space")
    if not isinstance(embedding_space, str) or not embedding_space.strip():
        raise ValueError("RAG result is missing embedding_space")
    if document_type == "POLICY":
        if source_type != "CURATED_INTERNAL":
            raise ValueError("POLICY evidence has an invalid source_type")
        if any(chunk.get(field) is not None for field in ("book_id", "catalog_ref", "isbn")):
            raise ValueError("POLICY evidence cannot contain book identity")
    else:
        if source_type != "CURATED_SYNTHETIC":
            raise ValueError("BOOK_GUIDE evidence has an invalid source_type")
        book_id = chunk.get("book_id")
        if type(book_id) is not int or book_id <= 0:
            raise ValueError("BOOK_GUIDE evidence requires a valid book_id")
        isbn = _required_text(chunk, "isbn")
        catalog_ref = _required_text(chunk, "catalog_ref")
        if expected_book_id is not None and book_id != expected_book_id:
            raise ValueError("BOOK_GUIDE evidence book_id does not match the requested Catalog evidence")
        if not re.fullmatch(r"^B[1-9][0-9]{0,3}$", catalog_ref):
            raise ValueError("BOOK_GUIDE evidence has an invalid catalog_ref")
        if not isbn.strip():
            raise ValueError("BOOK_GUIDE evidence is missing isbn")
    if expected_source_uri is not None and source_uri != expected_source_uri:
        raise ValueError("policy evidence source_uri does not match the cited source")
    if expected_section_path is not None and section_path != expected_section_path:
        raise ValueError("policy evidence section_path does not match the cited section")
    if expected_version is not None and version != expected_version:
        raise ValueError("policy evidence version does not match the cited version")


def evidence_from_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    """Project only fields that can be cited; vector/provider data never leaks."""
    # The Qdrant adapter adds point_id to the payload.  Validate before
    # projecting fields so callers cannot construct evidence from arbitrary
    # model output or partially populated dictionaries.
    validate_chunk_evidence(chunk)
    document_type = chunk.get("document_type")
    content_hash = chunk.get("content_hash")
    if not isinstance(content_hash, str) or not content_hash:
        raise ValueError("RAG result is missing content_hash")
    if document_type == "POLICY":
        evidence_id = policy_evidence_id(content_hash)
    elif document_type == "BOOK_GUIDE" and isinstance(chunk.get("book_id"), int):
        evidence_id = book_evidence_id(chunk["book_id"], content_hash)
    else:
        raise ValueError("RAG result has an invalid document identity")
    return {
        "evidence_id": evidence_id,
        "document_id": chunk.get("document_id"),
        "document_type": document_type,
        "book_id": chunk.get("book_id"),
        "isbn": chunk.get("isbn"),
        "document_title": chunk.get("document_title"),
        "section_path": chunk.get("section_path"),
        "version": chunk.get("version"),
        "effective_date": chunk.get("effective_date"),
        "locale": chunk.get("locale"),
        "source_type": chunk.get("source_type"),
        "source_uri": chunk.get("source_uri"),
        "catalog_ref": chunk.get("catalog_ref"),
        "chunk_index": chunk.get("chunk_index"),
        "point_id": chunk.get("point_id"),
        "embedding_space": chunk.get("embedding_space"),
        "content_hash": content_hash,
        "score": chunk.get("score"),
        "content": chunk.get("content"),
    }


def validate_book_evidence(chunk: dict[str, Any], allowed_book_ids: Iterable[int]) -> None:
    allowed = {value for value in allowed_book_ids if type(value) is int and value > 0}
    book_id = chunk.get("book_id")
    validate_chunk_evidence(chunk, expected_type="BOOK_GUIDE", expected_book_id=book_id if type(book_id) is int else None)
    if type(book_id) is not int or book_id not in allowed:
        raise ValueError("book evidence is outside the current Catalog evidence set")


def validate_policy_evidence(
    chunk: dict[str, Any],
    *,
    source_uri: str | None = None,
    section_path: str | None = None,
    version: str | None = None,
) -> None:
    """Strictly compare policy citation metadata with the real chunk."""
    validate_chunk_evidence(
        chunk,
        expected_type="POLICY",
        expected_source_uri=source_uri,
        expected_section_path=section_path,
        expected_version=version,
    )

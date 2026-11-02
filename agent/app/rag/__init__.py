"""Versioned, evidence-first RAG primitives for the v2 single-agent runtime.

The package deliberately has no startup side effects.  Indexing is an explicit
CLI operation and the online service only reads a previously audited manifest.
"""

from app.rag.contracts import (
    CatalogBook,
    RagChunk,
    RagDocument,
    RagDocumentLoader,
    RagManifest,
)

__all__ = [
    "CatalogBook",
    "RagChunk",
    "RagDocument",
    "RagDocumentLoader",
    "RagManifest",
]

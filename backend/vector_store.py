"""
Vector Store — ChromaDB-based knowledge base for semantic document search.

Per-tenant isolation: each tenant gets its own ChromaDB collection `kb_<tenant_id>`.

When a user clicks "Save to Knowledge Base", the document text is:
1. Split into overlapping chunks (~500 chars)
2. Embedded and stored in a persistent ChromaDB collection
3. Tagged with document ID + filename for citation tracking

Chat queries search across ALL saved documents for that tenant and return the most
relevant chunks along with source metadata for citations.
"""

import os
import threading
import chromadb
from chromadb.config import Settings
from config import Config
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter


# ---- Per-tenant collection cache (thread-safe) --------------------------------
_client = None
_client_lock = threading.Lock()
_collections: dict = {}
_collections_lock = threading.Lock()


def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            if Config.CHROMA_HOST:
                # Client/server mode — connect to separate ChromaDB container
                _client = chromadb.HttpClient(
                    host=Config.CHROMA_HOST,
                    port=Config.CHROMA_PORT,
                    settings=Settings(anonymized_telemetry=False),
                )
                print(f"🔗 ChromaDB connected to {Config.CHROMA_HOST}:{Config.CHROMA_PORT}")
            else:
                # Embedded mode — local PersistentClient (dev fallback)
                persist_dir = Config.CHROMADB_PATH
                os.makedirs(persist_dir, exist_ok=True)
                _client = chromadb.PersistentClient(
                    path=persist_dir,
                    settings=Settings(anonymized_telemetry=False),
                )
    return _client


def _get_collection(tenant_id: int):
    """Return (and cache) the ChromaDB collection for the given tenant."""
    with _collections_lock:
        if tenant_id not in _collections:
            client = _get_client()
            _collections[tenant_id] = client.get_or_create_collection(
                name=f"kb_{tenant_id}",
                metadata={"hnsw:space": "cosine"},
            )
        return _collections[tenant_id]


def _invalidate_collection_cache(tenant_id: int):
    """Force re-fetch of the collection on next access (e.g. after a reset)."""
    with _collections_lock:
        _collections.pop(tenant_id, None)


# ---- Chunking ----------------------------------------------------------------
DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 100

# Presets map: name -> (chunk_size_chars, overlap_chars)
CHUNK_PRESETS = {
    "small":  (200, 40),     # ~128-256 tokens
    "medium": (500, 100),    # ~512 tokens (default)
    "large":  (1000, 200),   # ~1024+ tokens
}


def _chunk_text(text: str, chunk_size: int = None, overlap: int = None) -> list[str]:
    """Split markdown text intelligently based on headers, then recursively by character size."""
    chunk_size = chunk_size or DEFAULT_CHUNK_SIZE
    overlap = overlap or DEFAULT_CHUNK_OVERLAP

    # 1. Split by Markdown headers (keeps logical sections together)
    headers_to_split_on = [
        ("#", "Header 1"),
        ("##", "Header 2"),
        ("###", "Header 3"),
        ("####", "Header 4"),
    ]
    md_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers_to_split_on, strip_headers=False)
    md_header_splits = md_splitter.split_text(text)

    # 2. Split any excessively large sections recursively
    recursive_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap,
    )
    final_splits = recursive_splitter.split_documents(md_header_splits)

    # 3. Format final string output, prepending header context so embedding model understands context
    chunks = []
    for doc in final_splits:
        # Build context string like "[Header 1 > Header 2]"
        context_parts = []
        for h in ["Header 1", "Header 2", "Header 3", "Header 4"]:
            if h in doc.metadata:
                context_parts.append(doc.metadata[h].strip())

        context_prefix = ""
        if context_parts:
            context_prefix = f"Context: {' > '.join(context_parts)}\n---\n"

        final_text = f"{context_prefix}{doc.page_content.strip()}".strip()
        if final_text:
            chunks.append(final_text)

    return chunks


# ---- Public API --------------------------------------------------------------

def add_document(tenant_id: int, doc_id: int, filename: str, text: str,
                 chunk_size: int = None, overlap: int = None) -> int:
    """Chunk and embed a document into the tenant's vector store. Returns chunk count."""
    col = _get_collection(tenant_id)

    # Remove any existing chunks for this doc (re-save scenario)
    remove_document(tenant_id, doc_id)

    chunks = _chunk_text(text, chunk_size=chunk_size, overlap=overlap)
    if not chunks:
        return 0

    ids = [f"doc{doc_id}_chunk{i}" for i in range(len(chunks))]
    metadatas = [
        {"doc_id": doc_id, "filename": filename, "chunk_index": i}
        for i in range(len(chunks))
    ]

    col.add(documents=chunks, ids=ids, metadatas=metadatas)
    print(f"📚 [tenant={tenant_id}] Indexed {len(chunks)} chunks for document {doc_id} ({filename})"
          f" [size={chunk_size or DEFAULT_CHUNK_SIZE}, overlap={overlap or DEFAULT_CHUNK_OVERLAP}]")
    return len(chunks)


def remove_document(tenant_id: int, doc_id: int):
    """Remove all chunks for a given document from the tenant's vector store."""
    col = _get_collection(tenant_id)
    try:
        col.delete(where={"doc_id": doc_id})
        print(f"🗑️ [tenant={tenant_id}] Removed chunks for document {doc_id}")
    except Exception as e:
        print(f"Warning: could not remove doc {doc_id} from vector store (tenant={tenant_id}): {e}")


def search(tenant_id: int, query: str, top_k: int = 6, filters: dict = None) -> list[dict]:
    """
    Search the tenant's knowledge base for chunks relevant to the query.
    filters: dict, e.g. {"filename_ne": "Book5.xlsx"} -> excludes that file.
    Returns list of dicts with keys: text, filename, doc_id, chunk_index, distance.
    """
    col = _get_collection(tenant_id)
    if col.count() == 0:
        return []

    where_clause = {}
    if filters and "filename_ne" in filters:
        where_clause = {"filename": {"$ne": filters["filename_ne"]}}

    results = col.query(
        query_texts=[query],
        n_results=min(top_k, col.count()),
        where=where_clause if where_clause else None
    )

    hits = []
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]

    for i in range(len(docs)):
        meta = metas[i] if i < len(metas) and metas[i] else {}
        hits.append({
            "text": docs[i],
            "filename": meta.get("filename", "unknown"),
            "doc_id": meta.get("doc_id", -1),
            "chunk_index": meta.get("chunk_index", i),
            "distance": dists[i] if i < len(dists) else None,
        })
    return hits


def get_stats(tenant_id: int) -> dict:
    """Return knowledge base statistics for a tenant."""
    col = _get_collection(tenant_id)
    total_chunks = col.count()

    if total_chunks == 0:
        return {"total_documents": 0, "total_chunks": 0, "documents": []}

    all_data = col.get(include=["metadatas"])
    doc_map = {}
    for meta in all_data["metadatas"]:
        if not meta:
            continue
        did = meta.get("doc_id")
        if did is None:
            continue
        if did not in doc_map:
            doc_map[did] = {"doc_id": did, "filename": meta.get("filename", "unknown"), "chunks": 0}
        doc_map[did]["chunks"] += 1

    return {
        "total_documents": len(doc_map),
        "total_chunks": total_chunks,
        "documents": list(doc_map.values()),
    }


def get_document_text(tenant_id: int, filename: str) -> str:
    """Retrieve full text of a document by filename from the tenant's KB."""
    col = _get_collection(tenant_id)
    results = col.get(where={"filename": filename}, include=["documents", "metadatas"])

    if not results['documents']:
        return ""

    chunks = []
    for doc, meta in zip(results['documents'], results['metadatas']):
        chunks.append((meta.get('chunk_index', 0), doc))

    chunks.sort(key=lambda x: x[0])
    return "\n".join([c[1] for c in chunks])

"""
Vector Store — ChromaDB-based knowledge base for semantic document search.

When a user clicks "Save to Knowledge Base", the document text is:
1. Split into overlapping chunks (~500 chars)
2. Embedded and stored in a persistent ChromaDB collection
3. Tagged with document ID + filename for citation tracking

Chat queries search across ALL saved documents and return the most
relevant chunks along with source metadata for citations.
"""

import os
import chromadb
from chromadb.config import Settings
from config import Config
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter


# ---- Singleton client --------------------------------------------------------
_client = None
_collection = None


def _get_collection():
    global _client, _collection
    if _collection is None:
        persist_dir = Config.CHROMADB_PATH
        os.makedirs(persist_dir, exist_ok=True)
        _client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False),
        )
        _collection = _client.get_or_create_collection(
            name="knowledge_base",
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


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

def add_document(doc_id: int, filename: str, text: str,
                 chunk_size: int = None, overlap: int = None) -> int:
    """Chunk and embed a document into the vector store. Returns chunk count."""
    col = _get_collection()

    # Remove any existing chunks for this doc (re-save scenario)
    remove_document(doc_id)

    chunks = _chunk_text(text, chunk_size=chunk_size, overlap=overlap)
    if not chunks:
        return 0

    ids = [f"doc{doc_id}_chunk{i}" for i in range(len(chunks))]
    metadatas = [
        {"doc_id": doc_id, "filename": filename, "chunk_index": i}
        for i in range(len(chunks))
    ]

    col.add(documents=chunks, ids=ids, metadatas=metadatas)
    print(f"📚 Indexed {len(chunks)} chunks for document {doc_id} ({filename})"
          f" [size={chunk_size or DEFAULT_CHUNK_SIZE}, overlap={overlap or DEFAULT_CHUNK_OVERLAP}]")
    return len(chunks)


def remove_document(doc_id: int):
    """Remove all chunks for a given document from the vector store."""
    col = _get_collection()
    try:
        # Direct delete by metadata filter is more efficient and safer
        col.delete(where={"doc_id": doc_id})
        print(f"🗑️ Removed chunks for document {doc_id}")
    except Exception as e:
        print(f"Warning: could not remove doc {doc_id} from vector store: {e}")


def search(query: str, top_k: int = 6, filters: dict = None) -> list[dict]:
    """
    Search the knowledge base for chunks relevant to the query.
    filters: dict, e.g. {"filename_ne": "Book5.xlsx"} -> excludes that file.
    Returns list of dicts with keys: text, filename, doc_id, chunk_index, distance.
    """
    col = _get_collection()
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


def get_stats() -> dict:
    """Return knowledge base statistics."""
    col = _get_collection()
    total_chunks = col.count()

    # Count unique documents
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


def get_document_text(filename: str) -> str:
    """Retrieve full text of a document by filename."""
    col = _get_collection()
    # Query by filename
    results = col.get(where={"filename": filename}, include=["documents", "metadatas"])
    
    if not results['documents']:
        return ""

    # Sort chunks by index to reconstruct text
    chunks = []
    for doc, meta in zip(results['documents'], results['metadatas']):
        chunks.append((meta.get('chunk_index', 0), doc))
    
    chunks.sort(key=lambda x: x[0])
    return "\n".join([c[1] for c in chunks])

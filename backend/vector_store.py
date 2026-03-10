"""
Vector Store — pgvector-based knowledge base for semantic document search.

Per-tenant isolation via dedicated tenant databases.
The db_name parameter tells the store which tenant database to connect to.

When a user clicks "Save to Knowledge Base", the document text is:
1. Split into overlapping chunks (markdown-aware)
2. Embedded via sentence-transformers (all-MiniLM-L6-v2, 384 dims)
3. Inserted into the kb_chunks table with pgvector

Chat queries search across ALL saved documents for that tenant using
cosine distance (vector <=> operator) and return the most relevant
chunks along with source metadata for citations.
"""

from models import KBChunk
from tenant_db import get_tenant_session
from embedding import embed_texts, embed_query
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from sqlalchemy import text as sa_text


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

    # 3. Format final string output, prepending header context
    chunks = []
    for doc in final_splits:
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

def add_document(db_name: str, tenant_id: int, doc_id: int, filename: str, text: str,
                 chunk_size: int = None, overlap: int = None) -> int:
    """Chunk, embed, and store a document in the tenant's KB. Returns chunk count."""
    db = get_tenant_session(db_name)
    try:
        # Remove any existing chunks for this doc (re-save scenario)
        remove_document(db_name, tenant_id, doc_id)

        chunks = _chunk_text(text, chunk_size=chunk_size, overlap=overlap)
        if not chunks:
            return 0

        # Batch embed all chunks
        embeddings = embed_texts(chunks)

        # Bulk insert
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            db.add(KBChunk(
                tenant_id=tenant_id,
                doc_id=doc_id,
                filename=filename,
                chunk_index=i,
                content=chunk,
                embedding=emb,
            ))

        db.commit()
        print(f"📚 [tenant={tenant_id}] Indexed {len(chunks)} chunks for document {doc_id} ({filename})"
              f" [size={chunk_size or DEFAULT_CHUNK_SIZE}, overlap={overlap or DEFAULT_CHUNK_OVERLAP}]")
        return len(chunks)
    finally:
        db.close()


def remove_document(db_name: str, tenant_id: int, doc_id: int):
    """Remove all chunks for a given document from the tenant's KB."""
    db = get_tenant_session(db_name)
    try:
        deleted = db.query(KBChunk).filter(
            KBChunk.tenant_id == tenant_id,
            KBChunk.doc_id == doc_id,
        ).delete()
        db.commit()
        if deleted:
            print(f"🗑️ [tenant={tenant_id}] Removed {deleted} chunks for document {doc_id}")
    except Exception as e:
        db.rollback()
        print(f"Warning: could not remove doc {doc_id} from vector store (tenant={tenant_id}): {e}")
    finally:
        db.close()


def search(db_name: str, tenant_id: int, query: str, top_k: int = 6, filters: dict = None) -> list[dict]:
    """
    Search the tenant's knowledge base for chunks relevant to the query.
    filters: dict, e.g. {"filename_ne": "Book5.xlsx"} -> excludes that file.
    Returns list of dicts with keys: text, filename, doc_id, chunk_index, distance.
    """
    db = get_tenant_session(db_name)
    try:
        # Check if any chunks exist
        count = db.query(KBChunk).filter(KBChunk.tenant_id == tenant_id).count()
        if count == 0:
            return []

        query_embedding = embed_query(query)

        # Build the SQL query with cosine distance
        sql = """
            SELECT content, filename, doc_id, chunk_index,
                   embedding <=> :query_vec AS distance
            FROM kb_chunks
            WHERE tenant_id = :tid
        """
        params = {"tid": tenant_id, "query_vec": str(query_embedding)}

        if filters and "filename_ne" in filters:
            sql += " AND filename != :exclude_fn"
            params["exclude_fn"] = filters["filename_ne"]

        sql += " ORDER BY distance LIMIT :top_k"
        params["top_k"] = top_k

        result = db.execute(sa_text(sql), params)

        hits = []
        for row in result:
            hits.append({
                "text": row[0],
                "filename": row[1] or "unknown",
                "doc_id": row[2] or -1,
                "chunk_index": row[3] or 0,
                "distance": float(row[4]) if row[4] is not None else None,
            })
        return hits
    finally:
        db.close()


def get_stats(db_name: str, tenant_id: int) -> dict:
    """Return knowledge base statistics for a tenant."""
    db = get_tenant_session(db_name)
    try:
        total_chunks = db.query(KBChunk).filter(KBChunk.tenant_id == tenant_id).count()

        if total_chunks == 0:
            return {"total_documents": 0, "total_chunks": 0, "documents": []}

        # Group by doc_id
        from sqlalchemy import func
        rows = db.query(
            KBChunk.doc_id,
            KBChunk.filename,
            func.count(KBChunk.id).label("chunks"),
        ).filter(
            KBChunk.tenant_id == tenant_id
        ).group_by(KBChunk.doc_id, KBChunk.filename).all()

        documents = [
            {"doc_id": r[0], "filename": r[1] or "unknown", "chunks": r[2]}
            for r in rows
        ]

        return {
            "total_documents": len(documents),
            "total_chunks": total_chunks,
            "documents": documents,
        }
    finally:
        db.close()


def get_document_text(db_name: str, tenant_id: int, filename: str) -> str:
    """Retrieve full text of a document by filename from the tenant's KB."""
    db = get_tenant_session(db_name)
    try:
        chunks = db.query(KBChunk).filter(
            KBChunk.tenant_id == tenant_id,
            KBChunk.filename == filename,
        ).order_by(KBChunk.chunk_index).all()

        if not chunks:
            return ""
        return "\n".join(c.content for c in chunks)
    finally:
        db.close()

"""
Framework Store — pgvector-based storage for compliance framework standards (per-tenant).

Each framework chunk is stored in the framework_chunks table with tenant_id
and framework_key for isolation.
"""

from models import SessionLocal, FrameworkChunk
from embedding import embed_texts, embed_query
from sqlalchemy import text as sa_text

FRAMEWORK_KEYS = ('CIS', 'GDPR', 'HIPAA', 'ISO27001', 'NIST', 'SOC2')


# ---- Chunking ----------------------------------------------------------------
CHUNK_SIZE = 800
CHUNK_OVERLAP = 150


def _chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks."""
    chunks = []
    start = 0
    text = text.strip()
    while start < len(text):
        end = start + CHUNK_SIZE
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk.strip())
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


# ---- Public API --------------------------------------------------------------

def add_framework(tenant_id: int, framework_key: str, version: str, filename: str, text: str) -> int:
    """Chunk, embed, and store a framework document. Returns chunk count."""
    db = SessionLocal()
    try:
        chunks = _chunk_text(text)
        if not chunks:
            return 0

        # Batch embed all chunks
        embeddings = embed_texts(chunks)

        for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            db.add(FrameworkChunk(
                tenant_id=tenant_id,
                framework_key=framework_key,
                version=version,
                filename=filename,
                chunk_index=i,
                content=chunk,
                embedding=emb,
            ))

        db.commit()
        print(f"📋 [tenant={tenant_id}] Indexed {len(chunks)} chunks for {framework_key} v{version} ({filename})")
        return len(chunks)
    finally:
        db.close()


def remove_framework(tenant_id: int, framework_key: str, version: str, filename: str):
    """Remove all chunks for a specific framework version/file."""
    db = SessionLocal()
    try:
        deleted = db.query(FrameworkChunk).filter(
            FrameworkChunk.tenant_id == tenant_id,
            FrameworkChunk.framework_key == framework_key,
            FrameworkChunk.version == version,
            FrameworkChunk.filename == filename,
        ).delete()
        db.commit()
        if deleted:
            print(f"🗑️ [tenant={tenant_id}] Removed {deleted} chunks for {framework_key} v{version} ({filename})")
    except Exception as e:
        db.rollback()
        print(f"Warning: could not remove framework chunks (tenant={tenant_id}): {e}")
    finally:
        db.close()


def search_framework(tenant_id: int, framework_key: str, query: str, top_k: int = 8) -> list[dict]:
    """Search within a tenant's specific framework for relevant sections."""
    db = SessionLocal()
    try:
        count = db.query(FrameworkChunk).filter(
            FrameworkChunk.tenant_id == tenant_id,
            FrameworkChunk.framework_key == framework_key,
        ).count()
        if count == 0:
            return []

        query_embedding = embed_query(query)

        sql = """
            SELECT content, version, filename,
                   embedding <=> :query_vec AS distance
            FROM framework_chunks
            WHERE tenant_id = :tid AND framework_key = :fk
            ORDER BY distance
            LIMIT :top_k
        """
        result = db.execute(sa_text(sql), {
            "tid": tenant_id,
            "fk": framework_key,
            "query_vec": str(query_embedding),
            "top_k": top_k,
        })

        hits = []
        for row in result:
            hits.append({
                "text": row[0],
                "version": row[1] or "unknown",
                "filename": row[2] or "unknown",
                "distance": float(row[3]) if row[3] is not None else None,
            })
        return hits
    finally:
        db.close()


def get_uploaded_frameworks(tenant_id: int) -> dict:
    """Return { framework_key: bool } indicating which frameworks have indexed content."""
    db = SessionLocal()
    try:
        status = {}
        for key in FRAMEWORK_KEYS:
            count = db.query(FrameworkChunk).filter(
                FrameworkChunk.tenant_id == tenant_id,
                FrameworkChunk.framework_key == key,
            ).count()
            status[key] = count > 0
        return status
    finally:
        db.close()

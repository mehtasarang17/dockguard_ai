"""
Framework Store — ChromaDB collections for compliance framework standards.

Each framework (CIS, GDPR, HIPAA, ISO27001, NIST, SOC2) gets its own
ChromaDB collection so that during analysis the orchestrator can retrieve
only the sections relevant to the specific framework being evaluated.
"""

import os
import chromadb
from chromadb.config import Settings
from config import Config

FRAMEWORK_KEYS = ('CIS', 'GDPR', 'HIPAA', 'ISO27001', 'NIST', 'SOC2')

# ---- Singleton client --------------------------------------------------------
_client = None


def _get_client():
    global _client
    if _client is None:
        persist_dir = os.path.join(Config.CHROMADB_PATH, "frameworks")
        os.makedirs(persist_dir, exist_ok=True)
        _client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False),
        )
    return _client


def _get_collection(framework_key: str):
    """Get or create the collection for a specific framework."""
    client = _get_client()
    return client.get_or_create_collection(
        name=f"framework_{framework_key}",
        metadata={"hnsw:space": "cosine"},
    )


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

def add_framework(framework_key: str, version: str, filename: str, text: str) -> int:
    """Chunk and embed a framework document. Returns chunk count."""
    col = _get_collection(framework_key)

    chunks = _chunk_text(text)
    if not chunks:
        return 0

    # Use version+filename for unique IDs so multiple versions coexist
    prefix = f"{framework_key}_{version}_{filename}"
    ids = [f"{prefix}_chunk{i}" for i in range(len(chunks))]
    metadatas = [
        {
            "framework_key": framework_key,
            "version": version,
            "filename": filename,
            "chunk_index": i,
        }
        for i in range(len(chunks))
    ]

    col.add(documents=chunks, ids=ids, metadatas=metadatas)
    print(f"📋 Indexed {len(chunks)} chunks for {framework_key} v{version} ({filename})")
    return len(chunks)


def remove_framework(framework_key: str, version: str, filename: str):
    """Remove all chunks for a specific framework version/file."""
    col = _get_collection(framework_key)
    try:
        # Delete by matching version AND filename
        col.delete(where={
            "$and": [
                {"version": {"$eq": version}},
                {"filename": {"$eq": filename}},
            ]
        })
        print(f"🗑️ Removed chunks for {framework_key} v{version} ({filename})")
    except Exception as e:
        print(f"Warning: could not remove framework chunks: {e}")


def search_framework(framework_key: str, query: str, top_k: int = 8) -> list[dict]:
    """Search within a specific framework's collection for relevant sections."""
    col = _get_collection(framework_key)
    if col.count() == 0:
        return []

    results = col.query(
        query_texts=[query],
        n_results=min(top_k, col.count()),
    )

    hits = []
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]

    for i in range(len(docs)):
        meta = metas[i] if i < len(metas) and metas[i] else {}
        hits.append({
            "text": docs[i],
            "version": meta.get("version", "unknown"),
            "filename": meta.get("filename", "unknown"),
            "distance": dists[i] if i < len(dists) else None,
        })
    return hits


def get_uploaded_frameworks() -> dict:
    """
    Return a dict: { framework_key: bool } indicating which frameworks
    have at least one document indexed.
    """
    client = _get_client()
    status = {}
    for key in FRAMEWORK_KEYS:
        try:
            col = client.get_or_create_collection(
                name=f"framework_{key}",
                metadata={"hnsw:space": "cosine"},
            )
            status[key] = col.count() > 0
        except Exception:
            status[key] = False
    return status

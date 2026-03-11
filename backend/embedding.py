"""
Embedding helper — generates vector embeddings using sentence-transformers.

Uses the same model as ChromaDB's default (all-mpnet-base-v2, 768 dims)
so embedding quality is identical to the previous ChromaDB setup.

The model is loaded as a singleton to avoid re-loading on every call.
Model files are cached in /root/.cache/torch/ (persisted via Docker volume).
"""

import threading
import numpy as np

_model = None
_model_lock = threading.Lock()

EMBEDDING_DIM = 768  # all-mpnet-base-v2 produces 768-dimensional embeddings


def _get_model():
    """Lazy-load the sentence-transformers model (singleton)."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                print("🔗 Loading embedding model: all-mpnet-base-v2 ...")
                _model = SentenceTransformer('all-mpnet-base-v2')
                print("✅ Embedding model loaded")
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Returns list of 768-dim float vectors."""
    if not texts:
        return []
    model = _get_model()
    embeddings = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
    return embeddings.tolist()


def embed_query(text: str) -> list[float]:
    """Embed a single query string. Returns a 768-dim float vector."""
    model = _get_model()
    embedding = model.encode(text, show_progress_bar=False, normalize_embeddings=True)
    return embedding.tolist()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    a_arr = np.array(a)
    b_arr = np.array(b)
    return float(np.dot(a_arr, b_arr) / (np.linalg.norm(a_arr) * np.linalg.norm(b_arr) + 1e-10))

"""
Chat File Store — Persistent session store for files uploaded in Knowledge Chat.

Uses a persistent ChromaDB collection (shared across Gunicorn workers) instead
of in-memory storage so all workers can access uploaded file data.

Sessions are stored on disk under UPLOAD_FOLDER/_chat_sessions/<session_id>/
with a metadata JSON file and a persistent ChromaDB collection.
"""

import os
import json
import time
import uuid
import shutil
import chromadb
from chromadb.config import Settings
from extractor import extract_text
from vector_store import _chunk_text, add_document, DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP
from config import Config

SESSION_TTL = 3600  # 1 hour
_SESSIONS_DIR = os.path.join(Config.UPLOAD_FOLDER, '_chat_sessions')


def _session_dir(session_id: str) -> str:
    return os.path.join(_SESSIONS_DIR, session_id)


def _cleanup_expired():
    """Remove sessions older than TTL."""
    if not os.path.exists(_SESSIONS_DIR):
        return
    now = time.time()
    for sid in os.listdir(_SESSIONS_DIR):
        meta_path = os.path.join(_SESSIONS_DIR, sid, 'meta.json')
        if os.path.exists(meta_path):
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
                if now - meta.get('created_at', 0) > SESSION_TTL:
                    clear_session(sid)
            except Exception:
                pass


def upload_file(file_path: str, filename: str) -> dict:
    """
    Extract, chunk, and index a file for temporary chat use.
    All data persisted to disk so all Gunicorn workers can access it.
    Returns { session_id, filename, chunk_count, char_count }.
    """
    _cleanup_expired()

    text = extract_text(file_path)
    if not text or not text.strip():
        raise ValueError("Could not extract any text from the uploaded file.")

    chunks = _chunk_text(text, chunk_size=DEFAULT_CHUNK_SIZE, overlap=DEFAULT_CHUNK_OVERLAP)
    if not chunks:
        raise ValueError("File produced no usable text chunks.")

    session_id = str(uuid.uuid4())
    sdir = _session_dir(session_id)
    os.makedirs(sdir, exist_ok=True)

    # Create persistent ChromaDB collection for this session
    chroma_dir = os.path.join(sdir, 'chroma')
    client = chromadb.PersistentClient(
        path=chroma_dir,
        settings=Settings(anonymized_telemetry=False),
    )
    collection = client.get_or_create_collection(
        name="chat_upload",
        metadata={"hnsw:space": "cosine"},
    )

    ids = [f"tmp_chunk_{i}" for i in range(len(chunks))]
    metadatas = [{"chunk_index": i, "filename": filename} for i in range(len(chunks))]
    collection.add(documents=chunks, ids=ids, metadatas=metadatas)

    # Persist the uploaded file so we can save to KB later
    file_copy = os.path.join(sdir, filename)
    shutil.copy2(file_path, file_copy)

    # Save metadata to disk
    meta = {
        'session_id': session_id,
        'filename': filename,
        'file_copy': file_copy,
        'text': text,
        'chunk_count': len(chunks),
        'char_count': len(text),
        'created_at': time.time(),
    }
    with open(os.path.join(sdir, 'meta.json'), 'w') as f:
        json.dump(meta, f)

    print(f"📎 Chat file uploaded: {filename} → {len(chunks)} chunks, session={session_id[:8]}")
    return {
        'session_id': session_id,
        'filename': filename,
        'chunk_count': len(chunks),
        'char_count': len(text),
    }


def _load_meta(session_id: str) -> dict | None:
    """Load session metadata from disk."""
    meta_path = os.path.join(_session_dir(session_id), 'meta.json')
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path) as f:
            return json.load(f)
    except Exception:
        return None


def _get_collection(session_id: str):
    """Get the ChromaDB collection for a session."""
    chroma_dir = os.path.join(_session_dir(session_id), 'chroma')
    if not os.path.exists(chroma_dir):
        return None
    client = chromadb.PersistentClient(
        path=chroma_dir,
        settings=Settings(anonymized_telemetry=False),
    )
    try:
        return client.get_collection("chat_upload")
    except Exception:
        return None


def search_file(session_id: str, query: str, top_k: int = 5) -> list[dict]:
    """Search the uploaded file's chunks for relevant content."""
    meta = _load_meta(session_id)
    if not meta:
        return []

    collection = _get_collection(session_id)
    if not collection or collection.count() == 0:
        return []

    results = collection.query(
        query_texts=[query],
        n_results=min(top_k, collection.count()),
    )

    hits = []
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]

    for i in range(len(docs)):
        m = metas[i] if i < len(metas) else {}
        hits.append({
            "text": docs[i],
            "filename": f"[Uploaded] {meta['filename']}",
            "doc_id": -1,
            "chunk_index": m.get("chunk_index", i),
            "distance": dists[i] if i < len(dists) else None,
            "source": "uploaded_file",
        })
    return hits


def get_session(session_id: str) -> dict | None:
    """Get session metadata."""
    meta = _load_meta(session_id)
    if not meta:
        return None
    return {
        'session_id': session_id,
        'filename': meta['filename'],
        'chunk_count': meta['chunk_count'],
        'char_count': meta['char_count'],
    }


def save_to_kb(session_id: str, db_session) -> dict:
    """
    Persist the uploaded file to the permanent Knowledge Base.
    Creates a Document record + indexes in the vector store.
    Returns { doc_id, filename, chunk_count }.
    """
    from models import Document

    meta = _load_meta(session_id)
    if not meta:
        raise ValueError("Session not found or expired.")

    filename = meta['filename']
    file_copy = meta['file_copy']

    if not os.path.exists(file_copy):
        raise ValueError("Uploaded file no longer available.")

    # Create permanent file path
    ts = time.strftime('%Y%m%d_%H%M%S')
    perm_filename = f"{ts}_{filename}"
    perm_path = os.path.join(Config.UPLOAD_FOLDER, perm_filename)
    shutil.copy2(file_copy, perm_path)

    file_size = os.path.getsize(perm_path)

    # Create DB record
    doc = Document(
        filename=perm_filename,
        original_filename=filename,
        file_path=perm_path,
        document_type='uploaded',
        file_size=file_size,
        status='completed',
        is_saved=True,
    )
    db_session.add(doc)
    db_session.flush()  # get doc.id

    # Index in vector store
    chunk_count = add_document(doc.id, filename, meta['text'])

    db_session.commit()

    # Clean up session
    clear_session(session_id)

    print(f"💾 Chat file saved to KB: {filename} → doc_id={doc.id}, {chunk_count} chunks")
    return {
        'doc_id': doc.id,
        'filename': filename,
        'chunk_count': chunk_count,
    }


def clear_session(session_id: str):
    """Remove a session and all its files."""
    sdir = _session_dir(session_id)
    if os.path.exists(sdir):
        try:
            shutil.rmtree(sdir)
        except Exception as e:
            print(f"Warning: could not clean session dir: {e}")
    print(f"🧹 Chat file session cleared: {session_id[:8]}")

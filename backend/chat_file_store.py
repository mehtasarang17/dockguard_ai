"""
Chat File Store — Persistent session store for files uploaded in Knowledge Chat.

Uses pgvector (chat_file_chunks table) for semantic search of uploaded files.
Sessions are stored on disk under UPLOAD_FOLDER/_chat_sessions/<session_id>/
with a metadata JSON file. Chunks + embeddings go into PostgreSQL.
"""

import os
import json
import time
import uuid
import shutil

from embedding import embed_texts, embed_query
from vector_store import _chunk_text, add_document, DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP
from models import SessionLocal, ChatFileChunk
from config import Config
from extractor import extract_text
from sqlalchemy import text as sa_text

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

    # Embed chunks and store in pgvector
    embeddings = embed_texts(chunks)
    db = SessionLocal()
    try:
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            db.add(ChatFileChunk(
                session_id=session_id,
                filename=filename,
                chunk_index=i,
                content=chunk,
                embedding=emb,
            ))
        db.commit()
    finally:
        db.close()

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


def search_file(session_id: str, query: str, top_k: int = 5) -> list[dict]:
    """Search the uploaded file's chunks for relevant content."""
    meta = _load_meta(session_id)
    if not meta:
        return []

    db = SessionLocal()
    try:
        count = db.query(ChatFileChunk).filter(
            ChatFileChunk.session_id == session_id
        ).count()
        if count == 0:
            return []

        query_embedding = embed_query(query)

        sql = """
            SELECT content, filename, chunk_index,
                   embedding <=> :query_vec AS distance
            FROM chat_file_chunks
            WHERE session_id = :sid
            ORDER BY distance
            LIMIT :top_k
        """
        result = db.execute(sa_text(sql), {
            "sid": session_id,
            "query_vec": str(query_embedding),
            "top_k": top_k,
        })

        hits = []
        for row in result:
            hits.append({
                "text": row[0],
                "filename": f"[Uploaded] {meta['filename']}",
                "doc_id": -1,
                "chunk_index": row[2] or 0,
                "distance": float(row[3]) if row[3] is not None else None,
                "source": "uploaded_file",
            })
        return hits
    finally:
        db.close()


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


def save_to_kb(session_id: str, db_session, tenant_id: int = 1) -> dict:
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
        tenant_id=tenant_id,
    )
    db_session.add(doc)
    db_session.flush()  # get doc.id

    # Index in vector store
    chunk_count = add_document(tenant_id, doc.id, filename, meta['text'])

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
    """Remove a session and all its files + DB chunks."""
    # Remove from database
    db = SessionLocal()
    try:
        db.query(ChatFileChunk).filter(
            ChatFileChunk.session_id == session_id
        ).delete()
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

    # Remove from disk
    sdir = _session_dir(session_id)
    if os.path.exists(sdir):
        try:
            shutil.rmtree(sdir)
        except Exception as e:
            print(f"Warning: could not clean session dir: {e}")
    print(f"🧹 Chat file session cleared: {session_id[:8]}")

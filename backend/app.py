import os
import json
import time
import threading
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.attributes import flag_modified
from werkzeug.utils import secure_filename

from config import Config
from models import init_db, get_db, SessionLocal, Document, Analysis, ChatHistory, SystemSettings, FrameworkStandard, BatchAnalysis, AuthorizedApp
from extractor import extract_text
from agents.orchestrator import Orchestrator
from agents.bedrock_client import BedrockClient
from agents.llm_factory import get_llm_client, get_active_provider, set_active_provider
from agents.prompts import knowledge_chat_prompt, standalone_question_prompt, framework_comparison_prompt, single_framework_llm_prompt
from sqlalchemy import func
import vector_store
import framework_store
import uuid

# ---- App Setup --------------------------------------------------------------
app = Flask(__name__)
app.config.from_object(Config)
CORS(app)

os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
init_db()

# Initialize default authorized app if none exist
def init_default_app():
    try:
        with SessionLocal() as db:
            count = db.query(AuthorizedApp).count()
            if count == 0:
                new_key = f"sk-{uuid.uuid4()}"
                app_entry = AuthorizedApp(name="Default Admin", api_key=new_key, is_active=True)
                db.add(app_entry)
                db.commit()
                print(f"🔑 Created default authorized app with key: {new_key}")
    except Exception as e:
        print(f"Error initializing default app: {e}")

init_default_app()

orchestrator = Orchestrator()
chat_llm = get_llm_client()

# ---- API Key Authentication Middleware ---------------------------------------
# Routes that do NOT require API key authentication
AUTH_EXEMPT_ROUTES = {
    'health',
}

@app.before_request
def require_api_key():
    """Enforce API key authentication on all /api/ routes."""
    # Skip non-API routes
    if not request.path.startswith('/api/'):
        return None

    # Skip exempted endpoints (health, app management — only reachable via frontend)
    if request.endpoint in AUTH_EXEMPT_ROUTES:
        return None

    # Allow frontend proxy requests with valid internal token
    internal_token = request.headers.get('X-Internal-Token', '')
    if internal_token and internal_token == Config.INTERNAL_TOKEN:
        return None

    # Check for API key in X-API-Key header or Authorization: Bearer
    api_key = request.headers.get('X-API-Key', '')
    if not api_key:
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            api_key = auth_header[7:]

    if not api_key:
        return jsonify({
            'error': 'Authentication required',
            'message': 'Please provide an API key via the X-API-Key header or Authorization: Bearer header.'
        }), 401

    # Validate against authorized apps
    db = get_db()
    try:
        app_entry = db.query(AuthorizedApp).filter_by(api_key=api_key).first()
        if not app_entry:
            return jsonify({
                'error': 'Invalid API key',
                'message': 'The provided API key is not valid.'
            }), 401
        if not app_entry.is_active:
            return jsonify({
                'error': 'Application disabled',
                'message': f'The application "{app_entry.name}" has been disabled by the administrator.'
            }), 403
        # Track last used
        app_entry.last_used = datetime.utcnow()
        db.commit()
    finally:
        db.close()

    return None


def _allowed(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in Config.ALLOWED_EXTENSIONS


def _increment_lifetime_tokens(db, token_count: int):
    """Atomically add token_count to the lifetime_tokens SystemSettings record."""
    if not token_count:
        return
    try:
        setting = db.query(SystemSettings).filter(SystemSettings.key == 'lifetime_tokens').first()
        if setting:
            setting.value = str(int(setting.value) + token_count)
        else:
            setting = SystemSettings(key='lifetime_tokens', value=str(token_count))
            db.add(setting)
        db.commit()
    except Exception as e:
        print(f"⚠️  Failed to update lifetime_tokens: {e}")


# ---- Background processing --------------------------------------------------
def _process_document(document_id: int, file_path: str, document_type: str):
    db = get_db()
    try:
        doc = db.query(Document).filter(Document.id == document_id).first()
        doc.status = 'processing'
        db.commit()

        text = extract_text(file_path)
        start = time.time()

        # Skip framework analysis during initial processing.
        # User selects frameworks to check after analysis completes.
        skip_all = {k: False for k in framework_store.FRAMEWORK_KEYS}

        result = orchestrator.run(document_id, text, document_type,
                                  uploaded_frameworks=skip_all)
        elapsed = time.time() - start

        scoring = result.get('scoring_details', {})

        analysis = Analysis(
            document_id=document_id,
            compliance_score=result.get('compliance_score', 0),
            security_score=result.get('security_score', 0),
            risk_score=result.get('risk_score', 0),
            overall_score=result.get('overall_score', 0),
            completeness_score=scoring.get('completeness', {}).get('score', 0),
            security_strength_score=scoring.get('security_strength', {}).get('score', 0),
            coverage_score=scoring.get('coverage', {}).get('score', 0),
            clarity_score=scoring.get('clarity', {}).get('score', 0),
            enforcement_score=scoring.get('enforcement_level', {}).get('score', 0),
            compliance_findings=result.get('compliance_findings', []),
            security_findings=result.get('security_findings', []),
            risk_findings=result.get('risk_findings', []),
            framework_mappings=result.get('framework_mappings', {}),
            gap_detections=result.get('gap_detections', []),
            best_practices=result.get('best_practices', []),
            suggestions=result.get('auto_suggestions', []),
            risk_level=result.get('risk_level', 'medium'),
            document_maturity=result.get('document_maturity', 'basic'),
            recommendations=result.get('recommendations', []),
            score_rationale=result.get('score_rationale', []),
            input_tokens=result.get('input_tokens', 0),
            output_tokens=result.get('output_tokens', 0),
            total_tokens=result.get('total_tokens', 0),
            processing_time=round(elapsed, 2),
        )
        db.add(analysis)
        doc.status = 'completed'
        db.commit()
        _increment_lifetime_tokens(db, result.get('total_tokens', 0))
        print(f"✅ Document {document_id} analysed in {elapsed:.1f}s")

    except Exception as e:
        print(f"❌ Error processing document {document_id}: {e}")
        import traceback; traceback.print_exc()
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc:
            doc.status = 'failed'
            db.commit()
    finally:
        db.close()


# ---- Background processing (batch) -------------------------------------------
def _process_batch(batch_id: int, documents_info: list, document_type: str):
    """Background worker: analyse multiple documents and run cross-doc synthesis."""
    db = get_db()
    try:
        batch = db.query(BatchAnalysis).filter(BatchAnalysis.id == batch_id).first()
        if not batch:
            return

        # Extract text for each document
        documents = []
        for info in documents_info:
            doc = db.query(Document).filter(Document.id == info['id']).first()
            if doc:
                doc.status = 'processing'
                db.commit()
                try:
                    text = extract_text(info['file_path'])
                    text_len = len(text.strip()) if text else 0
                    print(f"📄 Extracted text for '{info['filename']}': {text_len} chars")
                    if text_len == 0:
                        print(f"⚠️  Empty text for '{info['filename']}' — marking as failed")
                        doc.status = 'failed'
                        db.commit()
                        continue
                    documents.append({
                        'id': info['id'],
                        'filename': info['filename'],
                        'text': text,
                    })
                except Exception as e:
                    print(f"❌ Error extracting text for {info['filename']}: {e}")
                    doc.status = 'failed'
                    db.commit()

        if not documents:
            batch.status = 'failed'
            db.commit()
            return

        # Run batch analysis — use a fresh Orchestrator per batch so that
        # concurrent batch runs (multiple uploads) don't share a LangGraph
        # compiled graph instance across threads, which can corrupt iteration state.
        batch_orchestrator = Orchestrator()
        result = batch_orchestrator.run_batch(documents, document_type)

        # Save individual analyses
        for ir in result.get('individual_results', []):
            doc_id = ir['document_id']
            r = ir['result']
            doc = db.query(Document).filter(Document.id == doc_id).first()
            if not doc:
                continue

            # Extract scoring details
            scoring = r.get('scoring_details', {})

            analysis = Analysis(
                document_id=doc_id,
                compliance_score=r.get('compliance_score', 0),
                security_score=r.get('security_score', 0),
                risk_score=r.get('risk_score', 0),
                overall_score=r.get('overall_score', 0),
                completeness_score=scoring.get('completeness', {}).get('score', 0),
                security_strength_score=scoring.get('security_strength', {}).get('score', 0),
                coverage_score=scoring.get('coverage', {}).get('score', 0),
                clarity_score=scoring.get('clarity', {}).get('score', 0),
                enforcement_score=scoring.get('enforcement_level', {}).get('score', 0),
                compliance_findings=r.get('compliance_findings', []),
                security_findings=r.get('security_findings', []),
                risk_findings=r.get('risk_findings', []),
                framework_mappings=r.get('framework_mappings', {}),
                gap_detections=r.get('gap_detections', []),
                best_practices=r.get('best_practices', []),
                suggestions=r.get('auto_suggestions', []),
                risk_level=r.get('risk_level', 'medium'),
                document_maturity=r.get('document_maturity', 'basic'),
                recommendations=r.get('recommendations', []),
                score_rationale=r.get('score_rationale', []),
                input_tokens=r.get('input_tokens', 0),
                output_tokens=r.get('output_tokens', 0),
                total_tokens=r.get('total_tokens', 0),
                processing_time=r.get('processing_time', 0),
            )
            db.add(analysis)
            doc.status = 'completed'

        # Save batch results
        synthesis = result.get('synthesis', {})
        batch.overall_score = synthesis.get('overall_score', 0)
        batch.risk_level = synthesis.get('risk_level', 'medium')
        batch.document_maturity = synthesis.get('document_maturity', 'developing')
        batch.cross_doc_gaps = result.get('cross_doc_gaps', {})
        batch.synthesis = synthesis
        
        # Capture the computed total tokens properly into the synthesis JSON
        batch_tokens = result.get('total_tokens', 0)
        batch.synthesis['total_tokens'] = batch_tokens
        flag_modified(batch, "synthesis") # Tell SQLAlchemy the JSON changed

        batch.recommendations = synthesis.get('top_priorities', [])
        batch.score_rationale = synthesis.get('score_rationale', [])
        batch.processing_time = result.get('processing_time', 0)
        batch.status = 'completed'

        db.commit()
        
        # Increment global lifetime counter
        _increment_lifetime_tokens(db, batch_tokens)
        print(f"✅ Batch {batch_id} completed: {len(documents)} documents in {result.get('processing_time', 0):.1f}s")

    except Exception as e:
        print(f"❌ Batch {batch_id} failed: {e}")
        import traceback; traceback.print_exc()
        try:
            batch = db.query(BatchAnalysis).filter(BatchAnalysis.id == batch_id).first()
            if batch:
                batch.status = 'failed'
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


# ==============================================================================
# API ENDPOINTS
# ==============================================================================

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy', 'timestamp': datetime.utcnow().isoformat()})


# ---- LLM Provider Management ------------------------------------------------
from agents.ollama_client import OllamaClient

@app.route('/api/settings/llm-provider', methods=['GET'])
def get_provider():
    """Return current LLM provider and available options."""
    provider = get_active_provider()
    ollama_ok = OllamaClient.is_available()
    ollama_models = OllamaClient.list_models() if ollama_ok else []
    return jsonify({
        'provider': provider,
        'options': [
            {
                'key': 'bedrock',
                'name': 'AWS Bedrock',
                'description': 'Cloud-based (Amazon Nova / Claude)',
                'available': True,
            },
            {
                'key': 'ollama',
                'name': 'Ollama (Local)',
                'description': f'Free, runs locally — {Config.OLLAMA_MODEL}',
                'available': ollama_ok,
                'models': ollama_models,
                'configured_model': Config.OLLAMA_MODEL,
            },
        ],
    })


@app.route('/api/settings/llm-provider', methods=['POST'])
def set_provider():
    """Set the active LLM provider."""
    data = request.get_json(force=True)
    provider = data.get('provider', '').strip().lower()
    if provider not in ('bedrock', 'ollama'):
        return jsonify({'error': 'Invalid provider. Use "bedrock" or "ollama".'}), 400
    set_active_provider(provider)
    return jsonify({'provider': provider, 'message': f'Switched to {provider}'})


@app.route('/api/ollama/status', methods=['GET'])
def ollama_status():
    """Check if Ollama is reachable and whether the configured model is available."""
    available = OllamaClient.is_available()
    models = OllamaClient.list_models() if available else []
    configured = Config.OLLAMA_MODEL
    model_ready = any(configured in m for m in models)
    return jsonify({
        'available': available,
        'models': models,
        'configured_model': configured,
        'model_ready': model_ready,
    })


@app.route('/api/ollama/pull', methods=['POST'])
def ollama_pull():
    """Pull the configured Ollama model, streaming progress via SSE."""
    from flask import Response
    model = request.get_json(force=True).get('model', Config.OLLAMA_MODEL)

    def generate():
        for progress in OllamaClient.pull_model_stream(model):
            status = progress.get('status', '')
            total = progress.get('total', 0)
            completed = progress.get('completed', 0)
            pct = round((completed / total) * 100) if total else 0
            event = json.dumps({
                'status': status,
                'total': total,
                'completed': completed,
                'percent': pct,
            })
            yield f"data: {event}\n\n"
        # Final done event
        yield f"data: {json.dumps({'status': 'done', 'percent': 100})}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ---- Upload & Analyse -------------------------------------------------------
@app.route('/api/upload', methods=['POST'])
def upload_document():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    if not _allowed(file.filename):
        return jsonify({'error': 'File type not allowed. Use PDF, DOCX, or TXT'}), 400

    document_type = request.form.get('document_type', 'policy')
    allowed_types = ('policy', 'contract', 'procedure', 'security_policy',
                     'compliance', 'privacy', 'hr', 'it', 'other')
    if document_type not in allowed_types:
        document_type = 'policy'  # fallback to safe default

    original = file.filename
    safe = secure_filename(original)
    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    unique = f"{ts}_{safe}"
    path = os.path.join(Config.UPLOAD_FOLDER, unique)
    file.save(path)

    db = get_db()
    try:
        doc = Document(
            filename=unique,
            original_filename=original,
            file_path=path,
            document_type=document_type,
            file_size=os.path.getsize(path),
            status='uploaded',
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        doc_id = doc.id
    finally:
        db.close()

    t = threading.Thread(target=_process_document, args=(doc_id, path, document_type), daemon=True)
    t.start()

    return jsonify({'message': 'Document uploaded', 'document_id': doc_id, 'status': 'processing'}), 201


# ---- Documents CRUD ---------------------------------------------------------
@app.route('/api/history', methods=['GET'])
def get_history():
    """Returns combined history of all single and batch analyses, sorted by date."""
    db = get_db()
    try:
        docs = db.query(Document).join(Analysis).all()
        batches = db.query(BatchAnalysis).all()
        
        items = []
        for doc in docs:
            if not doc.analysis:
                continue
            items.append({
                'id': f"doc_{doc.id}",
                'real_id': doc.id,
                'type': 'single',
                'title': doc.filename,
                'date': doc.upload_date.isoformat() if doc.upload_date else None,
                'score': doc.analysis.overall_score,
                'risk_level': doc.analysis.risk_level,
                'maturity': doc.analysis.document_maturity,
                'time': doc.analysis.processing_time,
                'status': doc.status
            })
            
        for batch in batches:
            items.append({
                'id': f"batch_{batch.id}",
                'real_id': batch.id,
                'type': 'batch',
                'title': f"Batch Analysis ({len(batch.document_ids or [])} docs)",
                'date': batch.created_at.isoformat() if batch.created_at else None,
                'score': batch.overall_score,
                'risk_level': batch.risk_level,
                'maturity': batch.document_maturity,
                'time': batch.processing_time,
                'status': batch.status
            })
            
        items.sort(key=lambda x: x['date'] or '', reverse=True)
        return jsonify({'history': items})
    finally:
        db.close()


@app.route('/api/documents', methods=['GET'])
def list_documents():
    db = get_db()
    try:
        docs = db.query(Document).order_by(Document.upload_date.desc()).all()
        return jsonify({'documents': [d.to_dict() for d in docs]})
    finally:
        db.close()


@app.route('/api/documents/<int:doc_id>', methods=['GET'])
def get_document(doc_id):
    db = get_db()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            return jsonify({'error': 'Document not found'}), 404
        resp = {'document': doc.to_dict(), 'analysis': None}
        if doc.analysis:
            resp['analysis'] = doc.analysis.to_dict()
        return jsonify(resp)
    finally:
        db.close()


@app.route('/api/documents/<int:doc_id>', methods=['DELETE'])
def delete_document(doc_id):
    db = get_db()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            return jsonify({'error': 'Document not found'}), 404
        # Always remove from vector store (handles edge cases)
        try:
            vector_store.remove_document(doc_id)
        except Exception as e:
            print(f"Warning: vector store cleanup for doc {doc_id}: {e}")
        if os.path.exists(doc.file_path):
            os.remove(doc.file_path)
        db.delete(doc)
        db.commit()
        return jsonify({'message': 'Document deleted'})
    finally:
        db.close()

@app.route('/api/documents/<int:doc_id>/rename', methods=['PATCH'])
def rename_document(doc_id):
    """Rename a document."""
    db = get_db()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            return jsonify({'error': 'Document not found'}), 404
        new_name = (request.get_json(force=True) or {}).get('filename', '').strip()
        if not new_name:
            return jsonify({'error': 'Filename is required'}), 400
        doc.original_filename = new_name
        db.commit()
        return jsonify({'message': 'Renamed', 'filename': new_name})
    finally:
        db.close()


@app.route('/api/documents/<int:doc_id>/save', methods=['POST'])
def save_document(doc_id):
    """Save document to knowledge base: mark as saved + chunk & embed into vector store."""
    db = get_db()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            return jsonify({'error': 'Document not found'}), 404

        # Parse optional chunk config from request body
        body = request.get_json(silent=True) or {}
        preset = body.get('chunk_preset', 'medium')
        chunk_size, overlap = vector_store.CHUNK_PRESETS.get(
            preset, vector_store.CHUNK_PRESETS['medium']
        )
        # Allow explicit overrides
        chunk_size = body.get('chunk_size', chunk_size)
        overlap = body.get('overlap', overlap)

        # Extract text and add to vector store
        text = extract_text(doc.file_path)
        chunk_count = vector_store.add_document(
            doc_id, doc.original_filename, text,
            chunk_size=chunk_size, overlap=overlap,
        )

        doc.is_saved = True
        db.commit()
        return jsonify({
            'message': 'Document saved to knowledge base',
            'document': doc.to_dict(),
            'chunks_indexed': chunk_count,
            'chunk_preset': preset,
            'chunk_size': chunk_size,
            'overlap': overlap,
        })
    except Exception as e:
        print(f"Save to KB error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ---- Analysis ----------------------------------------------------------------
@app.route('/api/analysis/<int:doc_id>', methods=['GET'])
def get_analysis(doc_id):
    db = get_db()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            return jsonify({'error': 'Document not found'}), 404
        if not doc.analysis:
            return jsonify({'error': 'Analysis not available yet'}), 404
        return jsonify({'document': doc.to_dict(), 'analysis': doc.analysis.to_dict()})
    finally:
        db.close()


# ---- Framework Check (post-analysis) ----------------------------------------
@app.route('/api/analysis/<int:doc_id>/check-frameworks', methods=['POST'])
def check_frameworks(doc_id):
    """Run framework comparison for user-selected frameworks."""
    db = get_db()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            return jsonify({'error': 'Document not found'}), 404
        if not doc.analysis:
            return jsonify({'error': 'Analysis not available yet'}), 404

        body = request.get_json(force=True)
        selected = body.get('frameworks', [])
        valid_keys = list(framework_store.FRAMEWORK_KEYS)

        if selected == 'all' or selected == ['all']:
            selected = valid_keys
        else:
            selected = [k for k in selected if k in valid_keys]

        if not selected:
            return jsonify({'error': 'No valid frameworks selected'}), 400

        # Get document text
        text = extract_text(doc.file_path)
        fw_uploaded = framework_store.get_uploaded_frameworks()

        llm = get_llm_client()
        mappings = dict(doc.analysis.framework_mappings or {})

        for key in selected:
            try:
                if fw_uploaded.get(key, False):
                    # RAG comparison with uploaded standard
                    hits = framework_store.search_framework(key, text[:2000], top_k=8)
                    if hits:
                        prompt = framework_comparison_prompt(text, doc.document_type, key, hits)
                    else:
                        prompt = single_framework_llm_prompt(text, doc.document_type, key)
                    source = 'uploaded_standard'
                else:
                    # LLM knowledge-based comparison
                    prompt = single_framework_llm_prompt(text, doc.document_type, key)
                    source = 'ai_knowledge'

                raw = llm.invoke(prompt, max_tokens=6000)
                data = llm.parse_json(raw)
                data['source'] = source
                mappings[key] = data
                print(f"  ✅ {key}: score {data.get('alignment_score', '?')} (source: {source})")
            except Exception as e:
                print(f"  ❌ {key}: {e}")
                mappings[key] = {'alignment_score': 0, 'mapped_controls': [], 'error': str(e), 'source': 'ai_knowledge'}

        # Mark unselected frameworks as pending
        for key in valid_keys:
            if key not in selected and key not in mappings:
                mappings[key] = {'not_uploaded': True}

        # Persist
        doc.analysis.framework_mappings = mappings
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(doc.analysis, 'framework_mappings')
        db.commit()

        return jsonify({
            'framework_mappings': mappings,
            'uploaded_status': fw_uploaded,
        })
    except Exception as e:
        print(f"Framework check error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ---- Batch Upload & Analysis -------------------------------------------------
@app.route('/api/upload-batch', methods=['POST'])
def upload_batch():
    """Upload multiple documents for batch analysis with cross-doc synthesis."""
    files = request.files.getlist('files')
    if not files or len(files) == 0:
        return jsonify({'error': 'No files provided'}), 400
    if len(files) > 10:
        return jsonify({'error': 'Maximum 10 files per batch'}), 400

    document_type = request.form.get('document_type', 'policy')
    allowed_types = ('policy', 'contract', 'procedure', 'security_policy',
                     'compliance', 'privacy', 'hr', 'it', 'other')
    if document_type not in allowed_types:
        document_type = 'policy'

    db = get_db()
    try:
        documents_info = []
        for file in files:
            if file.filename == '' or not _allowed(file.filename):
                continue

            original = file.filename
            safe = secure_filename(original)
            ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            unique = f"{ts}_{safe}"
            path = os.path.join(Config.UPLOAD_FOLDER, unique)
            file.save(path)

            doc = Document(
                filename=unique,
                original_filename=original,
                file_path=path,
                document_type=document_type,
                file_size=os.path.getsize(path),
                status='uploaded',
            )
            db.add(doc)
            db.flush()  # Get the ID before commit
            documents_info.append({
                'id': doc.id,
                'filename': original,
                'file_path': path,
            })

        if not documents_info:
            return jsonify({'error': 'No valid files uploaded'}), 400

        # Create batch analysis record
        batch = BatchAnalysis(
            document_ids=[d['id'] for d in documents_info],
            document_type=document_type,
            status='processing',
        )
        db.add(batch)
        db.commit()
        db.refresh(batch)
        batch_id = batch.id

        # Start background processing
        t = threading.Thread(
            target=_process_batch,
            args=(batch_id, documents_info, document_type),
            daemon=True,
        )
        t.start()

        return jsonify({
            'message': f'{len(documents_info)} documents uploaded for batch analysis',
            'batch_id': batch_id,
            'document_ids': [d['id'] for d in documents_info],
            'status': 'processing',
        }), 201

    except Exception as e:
        print(f"Batch upload error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@app.route('/api/batch-analysis/<int:batch_id>', methods=['GET'])
def get_batch_analysis(batch_id):
    """Get batch analysis results including cross-doc synthesis."""
    db = get_db()
    try:
        batch = db.query(BatchAnalysis).filter(BatchAnalysis.id == batch_id).first()
        if not batch:
            return jsonify({'error': 'Batch not found'}), 404

        # Get individual document results
        individual = []
        for doc_id in (batch.document_ids or []):
            doc = db.query(Document).filter(Document.id == doc_id).first()
            if doc:
                doc_data = doc.to_dict()
                if doc.analysis:
                    doc_data['analysis'] = doc.analysis.to_dict()
                individual.append(doc_data)

        return jsonify({
            'batch': batch.to_dict(),
            'documents': individual,
        })
    finally:
        db.close()


@app.route('/api/batch-analysis/<int:batch_id>', methods=['DELETE'])
def delete_batch_analysis(batch_id):
    """Delete a batch analysis and all its associated documents."""
    db = get_db()
    try:
        batch = db.query(BatchAnalysis).filter(BatchAnalysis.id == batch_id).first()
        if not batch:
            return jsonify({'error': 'Batch not found'}), 404
            
        for doc_id in (batch.document_ids or []):
            doc = db.query(Document).filter(Document.id == doc_id).first()
            if doc:
                try:
                    vector_store.remove_document(doc_id)
                except Exception:
                    pass
                if os.path.exists(doc.file_path):
                    try:
                        os.remove(doc.file_path)
                    except:
                        pass
                db.delete(doc)
                
        db.delete(batch)
        db.commit()
        return jsonify({'message': 'Batch deleted'})
    finally:
        db.close()


@app.route('/api/batch-analysis/<int:batch_id>/save-all', methods=['POST'])
def save_batch_documents(batch_id):
    """Save all documents in a batch to the knowledge base."""
    db = get_db()
    try:
        batch = db.query(BatchAnalysis).filter(BatchAnalysis.id == batch_id).first()
        if not batch:
            return jsonify({'error': 'Batch not found'}), 404

        body = request.get_json(silent=True) or {}
        preset = body.get('chunk_preset', 'medium')
        chunk_size, overlap = vector_store.CHUNK_PRESETS.get(
            preset, vector_store.CHUNK_PRESETS['medium']
        )

        saved_docs = []
        total_chunks = 0
        for doc_id in (batch.document_ids or []):
            doc = db.query(Document).filter(Document.id == doc_id).first()
            if not doc or doc.is_saved:
                continue
            try:
                text = extract_text(doc.file_path)
                chunk_count = vector_store.add_document(
                    doc.id, doc.original_filename, text,
                    chunk_size=chunk_size, overlap=overlap,
                )
                doc.is_saved = True
                total_chunks += chunk_count
                saved_docs.append(doc.original_filename)
            except Exception as e:
                print(f"Error saving doc {doc_id} to KB: {e}")

        db.commit()
        return jsonify({
            'message': f'{len(saved_docs)} documents saved to knowledge base',
            'saved_documents': saved_docs,
            'total_chunks_indexed': total_chunks,
        })
    except Exception as e:
        print(f"Batch save error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ---- Trends ------------------------------------------------------------------
@app.route('/api/trends', methods=['GET'])
def get_trends():
    """Return historical scores for trend charts."""
    db = get_db()
    try:
        analyses = (
            db.query(Analysis)
            .join(Document)
            .order_by(Document.upload_date.asc())
            .all()
        )
        trend = []
        for a in analyses:
            trend.append({
                'date': a.analysis_date.isoformat() if a.analysis_date else None,
                'document_id': a.document_id,
                'filename': a.document.original_filename if a.document else '',
                'overall_score': a.overall_score,
                'compliance_score': a.compliance_score,
                'security_score': a.security_score,
                'risk_score': a.risk_score,
            })
        return jsonify({'trends': trend})
    finally:
        db.close()


# ---- Stats Endpoint ----------------------------------------------------------
@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Return application-wide statistics including lifetime token usage."""
    db = get_db()
    try:
        setting = db.query(SystemSettings).filter(SystemSettings.key == 'lifetime_tokens').first()
        lifetime_tokens = int(setting.value) if setting else 0
        
        # Calculate total tokens used explicitly in chat
        chat_tokens_query = db.query(func.sum(ChatHistory.tokens_used)).scalar()
        total_chat_tokens = int(chat_tokens_query) if chat_tokens_query else 0
        
        return jsonify({
            'lifetime_tokens': lifetime_tokens,
            'total_chat_tokens': total_chat_tokens
        })
    finally:
        db.close()

@app.route('/api/stats/reset', methods=['POST'])
def reset_tokens():
    """Reset lifetime and chat token counts to zero."""
    db = get_db()
    try:
        # Reset lifetime settings to 0
        setting = db.query(SystemSettings).filter(SystemSettings.key == 'lifetime_tokens').first()
        if setting:
            setting.value = "0"
            
        # Reset all past chat history tokens to 0 to prevent sum rebuilding
        db.query(ChatHistory).update({ChatHistory.tokens_used: 0})
        
        db.commit()
        return jsonify({'status': 'success', 'message': 'Token counts reset to 0'})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ---- Knowledge-Base Chat (Unified RAG) ---------------------------------------
import chat_file_store

@app.route('/api/chat/upload', methods=['POST'])
def chat_upload_file():
    """Upload a file for temporary use in chat (not saved to KB yet)."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'Empty filename'}), 400

    # Check size (20 MB limit)
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > 20 * 1024 * 1024:
        return jsonify({'error': 'File too large. Max 20 MB.'}), 413

    # Save temporarily to extract
    temp_dir = os.path.join(Config.UPLOAD_FOLDER, '_chat_temp')
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, file.filename)
    file.save(temp_path)

    try:
        result = chat_file_store.upload_file(temp_path, file.filename)
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    finally:
        # Remove the initial temp file (chat_file_store keeps its own copy)
        try:
            os.remove(temp_path)
        except Exception:
            pass


@app.route('/api/chat/save-file', methods=['POST'])
def chat_save_file_to_kb():
    """Save the uploaded chat file permanently to the Knowledge Base."""
    data = request.get_json(force=True) or {}
    session_id = data.get('session_id', '').strip()
    if not session_id:
        return jsonify({'error': 'session_id required'}), 400

    db = get_db()
    try:
        result = chat_file_store.save_to_kb(session_id, db)
        return jsonify(result)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    finally:
        db.close()


@app.route('/api/chat/clear-file', methods=['POST'])
def chat_clear_file():
    """Discard the uploaded chat file."""
    data = request.get_json(force=True) or {}
    session_id = data.get('session_id', '').strip()
    if session_id:
        chat_file_store.clear_session(session_id)
    return jsonify({'message': 'Cleared'})


import document_filler
from flask import send_file

@app.route('/api/chat/fill-document', methods=['POST'])
def chat_fill_document():
    """Fill an uploaded document's questions with answers from KB + LLM."""
    data = request.get_json(force=True) or {}
    session_id = data.get('session_id', '').strip()
    if not session_id:
        return jsonify({'error': 'session_id required'}), 400

    meta = chat_file_store._load_meta(session_id)
    if not meta:
        return jsonify({'error': 'Session not found or expired.'}), 404

    filename = meta['filename']
    file_path = meta['file_copy']
    if not os.path.exists(file_path):
        return jsonify({'error': 'Uploaded file no longer available.'}), 404

    ext = os.path.splitext(filename)[1].lower()
    if ext not in document_filler.SUPPORTED_FORMATS:
        return jsonify({'error': f'Unsupported format for filling: {ext}. Supported: {", ".join(document_filler.SUPPORTED_FORMATS)}'}), 400

    try:
        # Use the same LLM client as chat
        fill_llm = get_llm_client()

        result = document_filler.fill_document(file_path, filename, fill_llm)

        used_tokens = fill_llm.total_input_tokens + fill_llm.total_output_tokens

        # Track token usage
        db = get_db()
        try:
            _increment_lifetime_tokens(db, used_tokens)
        finally:
            db.close()

        return jsonify({
            'download_url': f'/api/chat/download/{result["output_filename"]}',
            'filename': result['output_filename'],
            'original_filename': filename,
            'stats': result['stats'],
            'qa_pairs': result.get('qa_pairs', []),
            'tokens_used': used_tokens,
        })
    except Exception as e:
        print(f"Fill document error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/chat/download/<filename>', methods=['GET'])
def chat_download_file(filename):
    """Serve a filled document for download."""
    filled_dir = os.path.join(Config.UPLOAD_FOLDER, '_filled')
    file_path = os.path.join(filled_dir, filename)

    if not os.path.exists(file_path):
        return jsonify({'error': 'File not found'}), 404

    return send_file(file_path, as_attachment=True, download_name=filename)


@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'JSON body required'}), 400

    message = data.get('message', '').strip()
    if not message:
        return jsonify({'error': 'message is required'}), 400

    session_id = data.get('session_id', '').strip() or None

    # ---- Batch Q&A Logic ----
    # Check if user is asking to answer questions from a file
    # Pattern: "answer (all)? questions (from|in) <filename>"
    import re
    # Improved regex to capture filename with extension, ignoring trailing text
    # Matches: "answer all the questions in Book5.xlsx", "list questions from Book5.xlsx"
    match = re.search(r"(?:answer|list|show|get|find)\s+(?:all\s+)?(?:the\s+)?questions\s+(?:from|in)\s+[\"']?([^\"'\s]+\.(?:xlsx|xls|csv|docx|doc|pdf|txt))[\"']?", message, re.IGNORECASE)
    if match:
        target_filename = match.group(1).strip()
        print(f"Batch Q&A triggered for file: {target_filename}")
        return process_batch_questions(target_filename, message)

    # ---- Normal Contextual RAG ----
    with SessionLocal() as db:
        try:
            # Get history FIRST (Fix: Order by desc to get RECENT, then reverse)
            history = (
                db.query(ChatHistory)
                .filter(ChatHistory.document_id.is_(None))
                .order_by(ChatHistory.timestamp.desc())
                .limit(10)
                .all()
            )
            # Restore chronological order
            history.reverse()
            history_dicts = [h.to_dict() for h in history]

            # Initialize a new LLM client for this request to ensure token counts are thread-safe
            chat_llm = get_llm_client()

            # Contextualize query
            search_query = message
            if history_dicts:
                print(f"Rewriting query with history ({len(history_dicts)} msgs)...")
                rewrite_prompt = standalone_question_prompt(history_dicts, message)
                rewritten = chat_llm.invoke(rewrite_prompt)
                # Basic cleanup if LLM returns "Standalone Question: ..." prefix
                if "Standalone Question:" in rewritten:
                    rewritten = rewritten.split("Standalone Question:")[-1].strip()
                search_query = rewritten
                print(f"Original: {message} -> Rewritten: {search_query}")

            # ---- Dual-source RAG: uploaded file + KB ----
            uploaded_file_context = None
            if session_id:
                # Get the full uploaded file text to include as context
                file_session = chat_file_store.get_session(session_id)
                if file_session:
                    meta = chat_file_store._load_meta(session_id)
                    if meta and meta.get('text'):
                        # Truncate to ~8000 chars to fit in context window
                        uploaded_file_context = meta['text'][:8000]

            if session_id and uploaded_file_context:
                # When a file is uploaded, search KB using the FILE CONTENT as queries
                # (not the user message like "answer all questions"), so we find relevant KB chunks.
                file_lines = [l.strip() for l in uploaded_file_context.split('\n') if l.strip() and len(l.strip()) > 10]
                # Increase query cap to cover up to 50 questions
                all_queries = [search_query] + file_lines[:50]

                # Search KB for each query, get top 2, and deduplicate
                seen_chunks = set()
                hits = []
                for q in all_queries:
                    # If we already have enough context chunks, stop querying to save time
                    if len(hits) >= 20:
                        break
                    
                    q_hits = vector_store.search(q, top_k=2)
                    for h in q_hits:
                        key = (h.get('doc_id'), h.get('chunk_index'))
                        if key not in seen_chunks and h.get('filename') and h['filename'] != 'unknown' and h.get('doc_id', -1) != -1:
                            seen_chunks.add(key)
                            hits.append(h)
                            if len(hits) >= 20:  # Strict cap to prevent context window explosion
                                break
                print(f"📎 File-upload KB search: {len(all_queries)} queries → {len(hits)} unique KB chunks")
            else:
                # Normal KB search with user message
                kb_hits = vector_store.search(search_query, top_k=8)
                hits = [h for h in kb_hits if (
                    h.get('filename') and h['filename'] != 'unknown' and h.get('doc_id', -1) != -1
                )]

            if not hits and not uploaded_file_context:
                no_info_msg = 'I could not find relevant information.'
                if not session_id:
                    no_info_msg += ' Try uploading a document or saving documents to the Knowledge Base.'
                return jsonify({
                    'answer': no_info_msg,
                    'citations': [],
                    'has_uploaded_file': bool(session_id),
                    'session_id': session_id,
                })

            # Save user message
            user_msg = ChatHistory(document_id=None, role='user', message=message)
            db.add(user_msg)
            db.commit()

            # Build prompt with retrieved chunks + citation instructions
            if uploaded_file_context:
                # --- Dedicated file-upload prompt with strict citation rules ---
                file_session = chat_file_store.get_session(session_id)
                fname = file_session['filename'] if file_session else 'uploaded file'

                # Build KB context with source labels
                kb_context_parts = []
                kb_source_names = set()
                for i, chunk in enumerate(hits):
                    src = chunk['filename']
                    kb_source_names.add(src)
                    kb_context_parts.append(f"[Chunk {i+1} | Source: {src}]\n{chunk['text']}")
                kb_context_str = "\n\n".join(kb_context_parts) if kb_context_parts else "(No KB results found)"

                kb_sources_list = "\n".join(f"  - {s}" for s in sorted(kb_source_names)) if kb_source_names else "  (none)"

                prompt = f"""You are a knowledge base assistant. The user uploaded a file and wants you to answer questions using the Knowledge Base documents.

KNOWLEDGE BASE DOCUMENTS (these are the ONLY valid source names for citations):
{kb_sources_list}

KNOWLEDGE BASE CONTEXT:
\"\"\"
{kb_context_str}
\"\"\"

UPLOADED FILE ({fname}) — read this to understand what the user is asking:
\"\"\"
{uploaded_file_context}
\"\"\"

USER MESSAGE: {message}

CITATION RULES AND INSTRUCTIONS (FOLLOW STRICTLY):
1. Try to answer each question using ONLY information from the KNOWLEDGE BASE CONTEXT above.
2. When you find the answer in the KB context, cite the EXACT filename from the list above using the format [Source: exact_filename_here]
3. You MUST use the EXACT filenames listed under "KNOWLEDGE BASE DOCUMENTS". Do NOT shorten, rename, or invent source names.
4. If a question CANNOT be answered from the KB context, you MAY use your general knowledge to answer it, but you MUST cite it as [Source: External Knowledge]
5. IMPORTANT FOR LARGE FILES: If the uploaded file contains a long list of questions (e.g., more than 10), DO NOT attempt to answer all 50+ questions at once. That will exceed your output limit. Instead, answer the FIRST 5 to 10 questions perfectly with citations, and then add a note saying:
"**Note:** To get answers for all remaining questions, please click the **Download as Filled File** button below. It will process every question individually and generate a complete file for you."
6. Be concise but thorough. Use bullet points for lists.
7. DO NOT use Markdown numbered lists (like "1. ", "2. ") for the questions. This breaks the UI formatting. Instead, use bold headers like **Q1:**, **Q2:**, etc.

Answer:"""
            else:
                prompt = knowledge_chat_prompt(hits, history_dicts, message)
            answer = chat_llm.invoke(prompt)
            
            # Calculate total tokens used across the rewrite + final answer
            used_tokens = chat_llm.total_input_tokens + chat_llm.total_output_tokens

            # Save assistant message
            assistant_msg = ChatHistory(document_id=None, role='assistant', message=answer, tokens_used=used_tokens)
            db.add(assistant_msg)
            db.commit()
            
            # Update global counter
            _increment_lifetime_tokens(db, used_tokens)

            # Extract unique source docs for citation metadata
            cited_docs = {}
            for h in hits:
                if h.get('filename'):
                    cited_docs[h['filename']] = True

            return jsonify({
                'answer': answer,
                'citations': list(cited_docs.keys()),
                'tokens_used': used_tokens,
                'has_uploaded_file': bool(session_id),
                'session_id': session_id,
            })

        except Exception as e:
            print(f"Chat error: {e}")
            print(f"Save to KB error: {e}")
            import traceback; traceback.print_exc()
            return jsonify({'error': str(e)}), 500



def process_batch_questions(filename: str, user_message: str):
    """Extract questions from a file and answer them one by one."""
    import json
    from agents.prompts import extract_questions_prompt, batch_question_answer_prompt

    # 1. Retrieve full text of the file
    print(f"Retrieving text for {filename}...")
    full_text = vector_store.get_document_text(filename)
        
    # helper to save history
    def save_and_return(answer_text, citations_list, used_tokens=0):
        # Save interaction to DB
        with SessionLocal() as db:
            # User message
            db.add(ChatHistory(
                role='user',
                message=user_message,
                document_id=None
            ))
            # Assistant message
            db.add(ChatHistory(
                role='assistant',
                message=answer_text,
                document_id=None,
                tokens_used=used_tokens
            ))
            db.commit()
            
            # Update global counter
            if used_tokens > 0:
                _increment_lifetime_tokens(db, used_tokens)
            
        return jsonify({
            'answer': answer_text,
            'citations': citations_list,
            'tokens_used': used_tokens
        })

    if not full_text:
        return save_and_return(f"I could not find the file '{filename}' in the knowledge base. Please check the exact filename.", [])

    # 2. Extract questions using LLM
    print("Extracting questions...")
    extract_prompt = extract_questions_prompt(full_text)
    try:
        response = chat_llm.invoke(extract_prompt)
        # Clean up Markdown code blocks if present
        if "```json" in response:
            response = response.split("```json")[1].split("```")[0].strip()
        elif "```" in response:
            response = response.split("```")[1].split("```")[0].strip()
            
        questions = json.loads(response)
    except Exception as e:
        print(f"Error extracting questions: {e}")
        return save_and_return(f"I found the file '{filename}', but I couldn't extract questions from it. Error: {str(e)}", [filename])

    if not isinstance(questions, list) or not questions:
        return save_and_return(f"I found the file '{filename}', but I didn't find any questions in it.", [filename])

    print(f"Found {len(questions)} questions: {questions}")

    # 3. Answer each question
    results = []
    all_citations = set()
    
    # Deduplicate questions while preserving order
    seen = set()
    unique_questions = []
    for q in questions:
        q_clean = q.strip()
        if q_clean and q_clean not in seen:
            seen.add(q_clean)
            unique_questions.append(q_clean)
    questions = unique_questions

    # Limit max questions to avoid timeouts
    MAX_QUESTIONS = 10
    if len(questions) > MAX_QUESTIONS:
        results.append(f"_Note: Processing first {MAX_QUESTIONS} of {len(questions)} questions found._\n")
        questions = questions[:MAX_QUESTIONS]

    for i, q in enumerate(questions):
        print(f"Processing Q{i+1}: {q}")
        # Search: Increase top_k and EXCLUDE the source file to avoid self-referencing
        hits = vector_store.search(q, top_k=10, filters={"filename_ne": filename})
        
        # Debug: Print sources to verify we are getting other files
        print(f"  -> Found {len(hits)} chunks. Sources: {[h['filename'] for h in hits]}")
        
        # Filter out chunks with missing/corrupted metadata
        valid_hits = [h for h in hits if h.get('filename') and h['filename'] != 'unknown' and h.get('doc_id', -1) != -1]

        if not valid_hits:
            results.append(f"**Q{i+1}: {q}**\nNo relevant information found in the knowledge base for this question.\n")
            continue

        # Build prompt using the same style as the working individual chat flow
        prompt = batch_question_answer_prompt(valid_hits, q)
        answer = chat_llm.invoke(prompt).strip()

        # Track cited filenames
        valid_filenames = {h['filename'] for h in valid_hits}
        
        results.append(f"**Q{i+1}: {q}**\n{answer}\n")
        
        # Track citations for the metadata return (optimistic)
        for fname in valid_filenames:
            if fname in answer:
                all_citations.add(fname)
        
        for h in hits:
            if h.get('filename'):
                all_citations.add(h['filename'])

    final_report = f"# Batch Q&A Report for {filename}\n\n" + "\n---\n".join(results)
    
    # Calculate total tokens used across the entire batch (extraction + answers)
    used_tokens = chat_llm.total_input_tokens + chat_llm.total_output_tokens
    
    return save_and_return(final_report, list(all_citations), used_tokens)


@app.route('/api/chat/history', methods=['GET'])
def chat_history():
    """Get global chat history."""
    db = get_db()
    try:
        msgs = (
            db.query(ChatHistory)
            .filter(ChatHistory.document_id.is_(None))
            .order_by(ChatHistory.timestamp.asc())
            .all()
        )
        return jsonify({'messages': [m.to_dict() for m in msgs]})
    finally:
        db.close()


@app.route('/api/chat/history', methods=['DELETE'])
def clear_chat_history():
    """Clear global chat history."""
    db = get_db()
    try:
        db.query(ChatHistory).filter(ChatHistory.document_id.is_(None)).delete()
        db.commit()
        return jsonify({'message': 'Chat history cleared'})
    finally:
        db.close()


@app.route('/api/kb/stats', methods=['GET'])
def kb_stats():
    """Return knowledge base statistics."""
    stats = vector_store.get_stats()
    return jsonify(stats)


# ---- KB Reindex --------------------------------------------------------------
import json as _json
import signal as _signal

_REINDEX_STATE_FILE = '/tmp/reindex_state.json'
_REINDEX_DEFAULT = {"status": "idle", "current": 0, "total": 0, "current_doc": "", "message": ""}
_reindex_cancel = threading.Event()


def _get_reindex_state():
    try:
        with open(_REINDEX_STATE_FILE, 'r') as f:
            return _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError):
        return dict(_REINDEX_DEFAULT)


def _set_reindex_state(**kwargs):
    state = _get_reindex_state()
    state.update(kwargs)
    with open(_REINDEX_STATE_FILE, 'w') as f:
        _json.dump(state, f)


# Clean up stale "running" state from previous crashed runs
_startup_state = _get_reindex_state()
if _startup_state.get("status") == "running":
    print("⚠️  Found stale reindex state from previous run — resetting to idle")
    _set_reindex_state(status="idle", message="Reset after restart", current_doc="")


def _reindex_worker():
    """Background worker: re-extract and re-index all saved documents."""
    _reindex_cancel.clear()
    db = get_db()
    try:
        docs = db.query(Document).filter(Document.is_saved == True).all()
        _set_reindex_state(total=len(docs), current=0)

        if not docs:
            _set_reindex_state(status="done", message="No saved documents to reindex.")
            return

        for i, doc in enumerate(docs, 1):
            # Check for cancellation (e.g. container shutting down)
            if _reindex_cancel.is_set():
                print("🛑 Reindex cancelled (shutdown)")
                _set_reindex_state(status="idle", message="Cancelled due to shutdown", current_doc="")
                return

            _set_reindex_state(
                current=i,
                current_doc=doc.original_filename,
                message=f"Indexing {doc.original_filename} ({i}/{len(docs)})",
            )
            print(f"🔄 Reindex [{i}/{len(docs)}] {doc.original_filename}")

            try:
                text = extract_text(doc.file_path)
                vector_store.add_document(doc.id, doc.original_filename, text)
            except Exception as e:
                print(f"   ⚠️  Failed to reindex doc {doc.id}: {e}")

        _set_reindex_state(status="done", message=f"Successfully reindexed {len(docs)} documents.", current_doc="")
    except Exception as e:
        print(f"Reindex error: {e}")
        import traceback; traceback.print_exc()
        _set_reindex_state(status="error", message=str(e))
    finally:
        db.close()


def _handle_shutdown(signum, frame):
    """Signal handler: cancel any running reindex before exit."""
    print(f"🛑 Received signal {signum}, cancelling reindex…")
    _reindex_cancel.set()

_signal.signal(_signal.SIGTERM, _handle_shutdown)
_signal.signal(_signal.SIGINT, _handle_shutdown)


@app.route('/api/kb/reindex', methods=['POST'])
def kb_reindex():
    """Start a background reindex of all saved KB documents."""
    state = _get_reindex_state()
    if state["status"] == "running":
        return jsonify({"error": "Reindex already in progress"}), 409

    _set_reindex_state(status="running", current=0, total=0, current_doc="", message="Starting reindex…")
    t = threading.Thread(target=_reindex_worker, daemon=True)
    t.start()
    return jsonify({"message": "Reindex started"}), 202


@app.route('/api/kb/reindex/status', methods=['GET'])
def kb_reindex_status():
    """Return current reindex progress."""
    return jsonify(_get_reindex_state())



# ---- Framework Standards API -------------------------------------------------

FRAMEWORK_UPLOAD_DIR = os.path.join(Config.UPLOAD_FOLDER, 'frameworks')
os.makedirs(FRAMEWORK_UPLOAD_DIR, exist_ok=True)


@app.route('/api/frameworks', methods=['GET'])
def list_frameworks():
    """List all uploaded framework standard documents, grouped by key."""
    db = get_db()
    try:
        standards = db.query(FrameworkStandard).order_by(
            FrameworkStandard.framework_key, FrameworkStandard.uploaded_at.desc()
        ).all()
        grouped = {}
        for s in standards:
            grouped.setdefault(s.framework_key, []).append(s.to_dict())
        return jsonify({'frameworks': grouped})
    finally:
        db.close()


@app.route('/api/frameworks/status', methods=['GET'])
def frameworks_status():
    """Quick check: which frameworks have at least one uploaded standard."""
    return jsonify(framework_store.get_uploaded_frameworks())


@app.route('/api/frameworks/upload', methods=['POST'])
def upload_framework():
    """Upload a framework standard document."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    file = request.files['file']
    fw_key = request.form.get('framework_key', '').strip()
    version = request.form.get('version', '').strip()

    if fw_key not in FrameworkStandard.VALID_KEYS:
        return jsonify({'error': f'Invalid framework_key. Must be one of: {FrameworkStandard.VALID_KEYS}'}), 400
    if not version:
        return jsonify({'error': 'Version is required'}), 400
    if not file.filename or not _allowed(file.filename):
        return jsonify({'error': 'Invalid file type'}), 400

    filename = secure_filename(file.filename)
    # Store in sub-directory per framework key
    fw_dir = os.path.join(FRAMEWORK_UPLOAD_DIR, fw_key)
    os.makedirs(fw_dir, exist_ok=True)
    file_path = os.path.join(fw_dir, f"{version}_{filename}")
    file.save(file_path)

    # Extract text and index into ChromaDB
    try:
        text = extract_text(file_path)
        chunk_count = framework_store.add_framework(fw_key, version, filename, text)
    except Exception as e:
        return jsonify({'error': f'Failed to process file: {e}'}), 500

    # Save to DB
    db = get_db()
    try:
        record = FrameworkStandard(
            framework_key=fw_key,
            version=version,
            filename=filename,
            file_path=file_path,
            chunk_count=chunk_count,
        )
        db.add(record)
        db.commit()
        return jsonify({'message': 'Framework uploaded', 'framework': record.to_dict()}), 201
    finally:
        db.close()


@app.route('/api/frameworks/<int:fw_id>', methods=['DELETE'])
def delete_framework(fw_id):
    """Delete a specific framework version."""
    db = get_db()
    try:
        record = db.query(FrameworkStandard).filter_by(id=fw_id).first()
        if not record:
            return jsonify({'error': 'Framework not found'}), 404

        # Remove from ChromaDB
        framework_store.remove_framework(record.framework_key, record.version, record.filename)

        # Remove file
        try:
            if os.path.exists(record.file_path):
                os.remove(record.file_path)
        except Exception:
            pass

        db.delete(record)
        db.commit()
        return jsonify({'message': 'Framework deleted'})
    finally:
        db.close()


# ---- System Settings API (backward compat) ----------------------------------

@app.route('/api/system/settings/api-key', methods=['GET'])
def get_api_key():
    """Return the first active app's key (backward compatibility)."""
    db = get_db()
    try:
        app_entry = db.query(AuthorizedApp).filter_by(is_active=True).first()
        if app_entry:
            return jsonify({'api_key': app_entry.api_key})
        return jsonify({'api_key': ''})
    finally:
        db.close()


@app.route('/api/system/settings/api-key/refresh', methods=['POST'])
def refresh_api_key():
    """Rotate the first app's key (backward compatibility)."""
    db = get_db()
    try:
        app_entry = db.query(AuthorizedApp).first()
        new_key = f"sk-{uuid.uuid4()}"
        if app_entry:
            app_entry.api_key = new_key
            db.commit()
        return jsonify({'api_key': new_key})
    finally:
        db.close()


# ---- Authorized Applications CRUD -------------------------------------------

@app.route('/api/system/apps', methods=['GET'])
def list_apps():
    """List all authorized applications."""
    db = get_db()
    try:
        apps = db.query(AuthorizedApp).order_by(AuthorizedApp.created_at.desc()).all()
        return jsonify({'apps': [a.to_dict() for a in apps]})
    finally:
        db.close()


@app.route('/api/system/apps', methods=['POST'])
def create_app():
    """Register a new authorized application."""
    data = request.get_json()
    name = (data or {}).get('name', '').strip()
    if not name:
        return jsonify({'error': 'Application name is required'}), 400

    db = get_db()
    try:
        new_key = f"sk-{uuid.uuid4()}"
        app_entry = AuthorizedApp(name=name, api_key=new_key, is_active=True)
        db.add(app_entry)
        db.commit()
        db.refresh(app_entry)
        return jsonify(app_entry.to_dict()), 201
    finally:
        db.close()


@app.route('/api/system/apps/<int:app_id>', methods=['DELETE'])
def delete_app(app_id):
    """Revoke and delete an authorized application."""
    db = get_db()
    try:
        app_entry = db.query(AuthorizedApp).get(app_id)
        if not app_entry:
            return jsonify({'error': 'Application not found'}), 404
        db.delete(app_entry)
        db.commit()
        return jsonify({'message': f'Application "{app_entry.name}" has been revoked.'})
    finally:
        db.close()


@app.route('/api/system/apps/<int:app_id>/toggle', methods=['PATCH'])
def toggle_app(app_id):
    """Enable or disable an authorized application."""
    db = get_db()
    try:
        app_entry = db.query(AuthorizedApp).get(app_id)
        if not app_entry:
            return jsonify({'error': 'Application not found'}), 404
        app_entry.is_active = not app_entry.is_active
        db.commit()
        return jsonify(app_entry.to_dict())
    finally:
        db.close()


# ==============================================================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=Config.DEBUG)

import os
import json
import time
import threading
from datetime import datetime

from flask import Flask, request, jsonify, g
from flask_cors import CORS
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.attributes import flag_modified
from werkzeug.utils import secure_filename

from config import Config
from models import (
    init_db, get_central_db, Document, Analysis, ChatHistory,
    SystemSettings, FrameworkStandard, BatchAnalysis, BatchDocument, AuthorizedApp, Tenant,
)
from crypto import hash_token
from tenant_db import get_tenant_session, create_tenant_database
from extractor import extract_text
from agents.orchestrator import Orchestrator
from agents.bedrock_client import BedrockClient
from agents.llm_factory import get_llm_client, get_active_provider, set_active_provider, clear_tenant_llm_cache
from agents.prompts import knowledge_chat_prompt, standalone_question_prompt, framework_comparison_prompt, single_framework_llm_prompt
from sqlalchemy import func
import vector_store
import framework_store
import uuid
from tasks import process_document_task, process_batch_task
from processing import _increment_lifetime_tokens

# ---- App Setup --------------------------------------------------------------
app = Flask(__name__)
app.config.from_object(Config)
CORS(app, origins=os.environ.get('CORS_ORIGINS', 'http://localhost:3001').split(','))

os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
init_db()

# Initialize default tenant + admin app if none exist
def init_default_app():
    """Ensure the default tenant exists with the correct admin key and db_name."""
    try:
        db = get_central_db()
        try:
            # Ensure a default tenant exists
            tenant = db.query(Tenant).filter_by(slug='default').first()
            if not tenant:
                tenant = Tenant(name='Default', slug='default', is_active=True)
                db.add(tenant)
                db.flush()  # get tenant.id before committing
                print(f"🏢 Created default tenant (id={tenant.id})")

            # Set db_name to the central database (backward compat — no migration)
            if not tenant.db_name:
                central_db_name = Config.DATABASE_URL.rsplit('/', 1)[1]
                tenant.db_name = central_db_name
                print(f"📦 Set default tenant db_name to '{central_db_name}'")

            # Enforce the Admin API Key from the environment
            expected_admin_key = Config.ADMIN_API_KEY
            expected_admin_hash = hash_token(expected_admin_key)
            
            # Find the existing default admin app (if any)
            admin_app = db.query(AuthorizedApp).filter_by(
                tenant_id=tenant.id, 
                name='Default Admin'
            ).first()

            if not admin_app:
                app_entry = AuthorizedApp(
                    tenant_id=tenant.id,
                    name='Default Admin',
                    api_key_hash=expected_admin_hash,
                    api_key_prefix=expected_admin_key[:10],
                    is_active=True,
                    is_admin=True,
                )
                db.add(app_entry)
                print(f"🔑 Created default admin app with key from .env")
            else:
                if admin_app.api_key_hash != expected_admin_hash:
                    admin_app.api_key_hash = expected_admin_hash
                    admin_app.api_key_prefix = expected_admin_key[:10]
                    admin_app.is_active = True
                    admin_app.is_admin = True
                    print(f"🔄 Rotated default admin key to match .env Configuration")
            
            # Migrate: attach existing stray keys to the default tenant
            db.query(AuthorizedApp).filter(AuthorizedApp.tenant_id == None).update(
                {'tenant_id': tenant.id}
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        print(f"Error initializing default app: {e}")

init_default_app()

orchestrator = Orchestrator()
chat_llm = get_llm_client()

# ---- API Key Authentication Middleware (see middleware.py) --------------------
from middleware import register_middleware
register_middleware(app)



def _tenant_id() -> int:
    """Return the tenant_id for the current request."""
    tid = getattr(g, 'tenant_id', None)
    if tid is not None:
        return tid
    # Fallback: look up the first active tenant dynamically (avoids hardcoded ID=1)
    from db import get_central_session
    from models import Tenant
    try:
        central_db = get_central_session()
        tenant = central_db.query(Tenant).filter_by(is_active=True).order_by(Tenant.id).first()
        central_db.close()
        if tenant:
            return tenant.id
    except Exception:
        pass
    return 1  # Last resort fallback


def _tenant_db_name() -> str:
    """Return the tenant's database name for the current request."""
    db_name = getattr(g, 'tenant_db_name', None)
    if db_name is not None:
        return db_name
    # Fallback: look up the first active tenant dynamically
    from db import get_central_session
    from models import Tenant
    try:
        central_db = get_central_session()
        tenant = central_db.query(Tenant).filter_by(is_active=True).order_by(Tenant.id).first()
        central_db.close()
        if tenant:
            return tenant.db_name
    except Exception:
        pass
    return Config.DATABASE_URL.rsplit('/', 1)[1]


def _get_tenant_db():
    """Return a SQLAlchemy session for the current tenant's database."""
    return get_tenant_session(_tenant_db_name())


def _require_admin():
    """Return a 403 response if the caller is not an admin, else None."""
    if not getattr(g, 'is_admin', False):
        return jsonify({'error': 'Admin access required'}), 403
    return None


def _allowed(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in Config.ALLOWED_EXTENSIONS


# NOTE: _process_document and _process_batch moved to processing.py
# They are dispatched via Celery tasks (see tasks.py)


# ==============================================================================
# API ENDPOINTS
# ==============================================================================

@app.route('/apispec.json', methods=['GET'])
def apispec():
    """Serve the OpenAPI spec as JSON for Swagger UI."""
    import yaml
    spec_path = os.path.join(os.path.dirname(__file__), 'swagger.yml')
    with open(spec_path, 'r') as f:
        spec = yaml.safe_load(f)
    return jsonify(spec)


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy', 'timestamp': datetime.utcnow().isoformat()})


@app.route('/api/provision', methods=['POST'])
def provision():
    """Auto-provision a new tenant.

    Called by the SaaS backend when a new customer signs up.
    Creates the tenant, its first API key (with expiration), and a refresh token.

    Optional LLM configuration can be provided:
    - aws_bearer_token: AWS Bedrock bearer token (will be encrypted)
    - aws_region: AWS region (default: us-east-1)
    - bedrock_model_id: Bedrock model ID (default: Config.BEDROCK_MODEL_ID)

    If LLM fields are provided, credentials are validated before saving.
    """
    from datetime import timedelta
    from crypto import encrypt_value

    # --- Payload ---
    data = request.get_json(force=True) or {}
    name = data.get('name', '').strip()
    slug = data.get('slug', '').strip().lower().replace(' ', '-')
    expires_in_days = int(data.get('expires_in_days', 90))
    if not name or not slug:
        return jsonify({'error': 'name and slug are required'}), 400

    # Optional LLM configuration
    llm_bearer_token = data.get('aws_bearer_token', '').strip()
    llm_region = data.get('aws_region', 'us-east-1').strip()
    llm_model_id = data.get('bedrock_model_id', Config.BEDROCK_MODEL_ID).strip()

    # Validate LLM credentials if provided
    llm_configured = False
    if llm_bearer_token:
        valid, error_msg = _validate_bedrock_credentials(llm_bearer_token, llm_region, llm_model_id)
        if not valid:
            return jsonify({
                'error': 'Invalid LLM credentials',
                'message': error_msg,
            }), 400

    db = get_central_db()
    try:
        # Idempotent: if the slug already exists, return the existing tenant
        existing = db.query(Tenant).filter_by(slug=slug).first()
        if existing:
            existing_key = db.query(AuthorizedApp).filter_by(
                tenant_id=existing.id, is_active=True
            ).first()
            return jsonify({
                'error': 'Tenant already exists',
                'message': f'Tenant {slug} already exists. API key was shown only at creation time.',
                'tenant': existing.to_dict(),
                'api_key_prefix': existing_key.api_key_prefix if existing_key else None,
                'expires_at': existing_key.expires_at.isoformat() if existing_key and existing_key.expires_at else None,
                'is_expired': existing_key.is_expired if existing_key else False,
                'llm_configured': existing.has_llm_config,
            }), 409

        # Create tenant
        tenant = Tenant(name=name, slug=slug, is_active=True)
        db.add(tenant)
        db.flush()

        # Create a dedicated database for this tenant
        tenant_db_name = create_tenant_database(slug)
        tenant.db_name = tenant_db_name

        # Set LLM config if provided
        if llm_bearer_token:
            tenant.llm_aws_bearer_token = encrypt_value(llm_bearer_token)
            tenant.llm_aws_region = llm_region
            tenant.llm_bedrock_model_id = llm_model_id
            tenant.llm_config_updated_at = datetime.utcnow()
            llm_configured = True

        # Create first API key with expiration + refresh token
        first_key = f"sk-{uuid.uuid4()}"
        refresh_tok = f"rt-{uuid.uuid4()}"
        expires_at = datetime.utcnow() + timedelta(days=expires_in_days)

        app_entry = AuthorizedApp(
            tenant_id=tenant.id,
            name=f"{name} — Default Key",
            api_key_hash=hash_token(first_key),
            api_key_prefix=first_key[:10],
            refresh_token_hash=hash_token(refresh_tok),
            is_active=True,
            is_admin=False,
            expires_at=expires_at,
        )
        db.add(app_entry)
        db.commit()
        db.refresh(tenant)

        return jsonify({
            'tenant': tenant.to_dict(),
            'api_key': first_key,
            'expires_at': expires_at.isoformat(),
            'llm_configured': llm_configured,
            'created': True,
            'message': f'Tenant {name} provisioned successfully. When your API key expires, call POST /api/get-refresh-token with the expired key to obtain a short-lived refresh token.',
        }), 201
    finally:
        db.close()


@app.route('/api/get-refresh-token', methods=['POST'])
def get_refresh_token():
    """Issue a short-lived refresh token for an expired API key.

    The caller presents their expired API key. If valid (but expired),
    a new refresh token is generated with a 10-minute lifespan.
    The refresh token can then be used at POST /api/refresh-key to
    obtain a new API key.
    """
    from datetime import timedelta

    data = request.get_json(force=True) or {}
    api_key = data.get('api_key', '').strip()

    if not api_key:
        return jsonify({'error': 'api_key is required'}), 400

    db = get_central_db()
    try:
        app_entry = db.query(AuthorizedApp).filter_by(api_key_hash=hash_token(api_key)).first()
        if not app_entry:
            return jsonify({
                'error': 'Invalid API key',
                'message': 'API key not recognised.',
            }), 401
        if not app_entry.is_active:
            return jsonify({
                'error': 'Application disabled',
                'message': 'This application has been disabled by an administrator.',
            }), 403
        if not app_entry.is_expired:
            return jsonify({
                'error': 'API key not expired',
                'message': 'Your API key is still valid. Refresh tokens are only issued for expired keys.',
            }), 400

        # Generate a short-lived refresh token (10 minutes)
        refresh_tok = f"rt-{uuid.uuid4()}"
        app_entry.refresh_token_hash = hash_token(refresh_tok)
        app_entry.refresh_token_expires_at = datetime.utcnow() + timedelta(minutes=10)
        db.commit()

        return jsonify({
            'refresh_token': refresh_tok,
            'refresh_token_expires_at': app_entry.refresh_token_expires_at.isoformat(),
            'message': 'Refresh token issued. Use it at POST /api/refresh-key within 10 minutes to obtain a new API key.',
        }), 200
    finally:
        db.close()


@app.route('/api/refresh-key', methods=['POST'])
def provision_refresh_key():
    """Rotate an expired API key using a refresh token.

    Generates a new API key and resets the expiration.
    The refresh token is single-use and invalidated after use.
    """
    from datetime import timedelta

    data = request.get_json(force=True) or {}
    refresh_token = data.get('refresh_token', '').strip()
    new_expires_in_days = int(data.get('expires_in_days', 90))

    if not refresh_token:
        return jsonify({'error': 'refresh_token is required'}), 400

    db = get_central_db()
    try:
        app_entry = db.query(AuthorizedApp).filter_by(refresh_token_hash=hash_token(refresh_token)).first()
        if not app_entry:
            return jsonify({
                'error': 'Invalid refresh token',
                'message': 'Refresh token not found or already used.',
            }), 401
        if not app_entry.is_active:
            return jsonify({
                'error': 'Application disabled',
                'message': 'This application has been disabled by an administrator.',
            }), 403

        # Check refresh token expiration
        if app_entry.refresh_token_expires_at and app_entry.refresh_token_expires_at < datetime.utcnow():
            # Invalidate the expired refresh token
            app_entry.refresh_token_hash = None
            app_entry.refresh_token_expires_at = None
            db.commit()
            return jsonify({
                'error': 'Refresh token expired',
                'message': 'This refresh token has expired. Request a new one at POST /api/get-refresh-token with your expired API key.',
            }), 401

        # Rotate: new API key + invalidate refresh token + reset expiry
        old_prefix = app_entry.api_key_prefix or 'sk-??????'
        new_api_key = f"sk-{uuid.uuid4()}"
        app_entry.api_key_hash = hash_token(new_api_key)
        app_entry.api_key_prefix = new_api_key[:10]
        app_entry.refresh_token_hash = None  # invalidate — single use
        app_entry.refresh_token_expires_at = None
        app_entry.expires_at = datetime.utcnow() + timedelta(days=new_expires_in_days)
        app_entry.last_used = None  # reset
        db.commit()

        # Get tenant for LLM config status
        tenant = db.query(Tenant).filter(Tenant.id == app_entry.tenant_id).first()

        return jsonify({
            'api_key': new_api_key,
            'expires_at': app_entry.expires_at.isoformat(),
            'tenant_id': app_entry.tenant_id,
            'llm_configured': tenant.has_llm_config if tenant else False,
            'message': f'API key rotated successfully. Old key ({old_prefix}...) is now invalid.',
        }), 200
    finally:
        db.close()



@app.route('/api/verify-key', methods=['POST'])
def verify_api_key_info():
    """Verify an API key and return the tenant name.

    Called by the SaaS app to check if a key is valid and active.
    Returns tenant info or an error if expired/invalid.
    """
    data = request.get_json(force=True) or {}
    api_key = data.get('api_key', '').strip()
    if not api_key:
        return jsonify({'error': 'api_key is required'}), 400

    db = get_central_db()
    try:
        app_entry = db.query(AuthorizedApp).filter_by(api_key_hash=hash_token(api_key)).first()
        if not app_entry:
            return jsonify({
                'valid': False,
                'error': 'Invalid API key',
                'message': 'API key not recognised.',
            }), 401

        tenant = db.query(Tenant).filter_by(id=app_entry.tenant_id).first()
        tenant_name = tenant.name if tenant else 'Unknown'
        tenant_slug = tenant.slug if tenant else 'unknown'

        if not app_entry.is_active:
            return jsonify({
                'valid': False,
                'error': 'API key disabled',
                'message': 'This API key has been disabled by an administrator.',
                'tenant_name': tenant_name,
                'tenant_slug': tenant_slug,
            }), 403

        if app_entry.expires_at and app_entry.expires_at < datetime.utcnow():
            return jsonify({
                'valid': False,
                'error': 'API key expired',
                'message': 'Your API key has expired. Use your refresh token at POST /api/refresh-key to generate a new one.',
                'tenant_name': tenant_name,
                'tenant_slug': tenant_slug,
                'expired_at': app_entry.expires_at.isoformat(),
            }), 401

        return jsonify({
            'valid': True,
            'tenant_name': tenant_name,
            'tenant_slug': tenant_slug,
            'tenant_id': app_entry.tenant_id,
            'expires_at': app_entry.expires_at.isoformat() if app_entry.expires_at else None,
            'is_admin': bool(app_entry.is_admin),
            'llm_configured': tenant.has_llm_config if tenant else False,
        }), 200
    finally:
        db.close()


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


# ---- Per-Tenant LLM Configuration -------------------------------------------
import requests as http_requests
from crypto import encrypt_value, decrypt_value


def _validate_bedrock_credentials(bearer_token: str, region: str, model_id: str = None) -> tuple:
    """Validate AWS Bedrock credentials by making a minimal API call.

    Returns:
        (success: bool, error_message: str or None)
    """
    if not bearer_token or not region:
        return False, "Bearer token and region are required"

    # Use default model if not specified
    test_model = model_id or Config.BEDROCK_MODEL_ID

    url = f"https://bedrock-runtime.{region}.amazonaws.com/model/{test_model}/converse"
    headers = {
        'Authorization': f'Bearer {bearer_token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    payload = {
        "messages": [{"role": "user", "content": [{"text": "Hello"}]}],
        "inferenceConfig": {"maxTokens": 10, "temperature": 0.1},
    }

    try:
        resp = http_requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.ok:
            return True, None
        elif resp.status_code == 401 or resp.status_code == 403:
            return False, "Invalid or expired bearer token"
        elif resp.status_code == 404:
            return False, f"Model '{test_model}' not found or not enabled in region '{region}'"
        else:
            error_detail = resp.text[:200] if resp.text else "Unknown error"
            return False, f"Bedrock API error ({resp.status_code}): {error_detail}"
    except http_requests.exceptions.Timeout:
        return False, "Connection timeout - check region and network"
    except http_requests.exceptions.ConnectionError:
        return False, f"Cannot connect to Bedrock in region '{region}'"
    except Exception as e:
        return False, f"Validation failed: {str(e)}"


@app.route('/api/tenant/llm-config', methods=['GET'])
def get_tenant_llm_config():
    """Get tenant LLM configuration status (NOT credentials).

    Returns whether LLM config is set, the region, model, and last updated time.
    Does NOT return the bearer token for security reasons.
    """
    db = get_central_db()
    try:
        tenant = db.query(Tenant).filter(Tenant.id == _tenant_id()).first()
        if not tenant:
            return jsonify({'error': 'Tenant not found'}), 404

        return jsonify({
            'has_config': tenant.has_llm_config,
            'aws_region': tenant.llm_aws_region,
            'bedrock_model_id': tenant.llm_bedrock_model_id,
            'bearer_token_set': bool(tenant.llm_aws_bearer_token),
            'aws_bearer_token': decrypt_value(tenant.llm_aws_bearer_token) if tenant.llm_aws_bearer_token else None,
            'updated_at': tenant.llm_config_updated_at.isoformat() if tenant.llm_config_updated_at else None,
        })
    finally:
        db.close()


@app.route('/api/tenant/llm-config', methods=['POST'])
def set_tenant_llm_config():
    """Set or update tenant LLM configuration.

    Validates credentials before saving. Encrypts the bearer token at rest.
    """
    data = request.get_json(force=True) or {}

    bearer_token = data.get('aws_bearer_token', '').strip()
    aws_region = data.get('aws_region', 'us-east-1').strip()
    model_id = data.get('bedrock_model_id', Config.BEDROCK_MODEL_ID).strip()

    if not bearer_token:
        return jsonify({'error': 'aws_bearer_token is required'}), 400
    if not aws_region:
        return jsonify({'error': 'aws_region is required'}), 400

    # Validate credentials
    valid, error_msg = _validate_bedrock_credentials(bearer_token, aws_region, model_id)
    if not valid:
        return jsonify({
            'error': 'Invalid credentials',
            'message': error_msg,
        }), 400

    # Save encrypted config
    db = get_central_db()
    try:
        tenant = db.query(Tenant).filter(Tenant.id == _tenant_id()).first()
        if not tenant:
            return jsonify({'error': 'Tenant not found'}), 404

        tenant.llm_aws_bearer_token = encrypt_value(bearer_token)
        tenant.llm_aws_region = aws_region
        tenant.llm_bedrock_model_id = model_id
        tenant.llm_config_updated_at = datetime.utcnow()
        db.commit()

        # Clear cached LLM client for this tenant
        clear_tenant_llm_cache(_tenant_id())

        return jsonify({
            'message': 'LLM configuration saved successfully',
            'has_config': True,
            'aws_region': aws_region,
            'bedrock_model_id': model_id,
        })
    finally:
        db.close()


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

    # Validate LLM credentials before accepting the file
    try:
        get_llm_client(tenant_id=_tenant_id())
    except RuntimeError as e:
        return jsonify({'error': 'LLM not configured', 'message': str(e)}), 402

    original = file.filename
    safe = secure_filename(original)
    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    unique = f"{ts}_{safe}"
    tenant_dir = os.path.join(Config.UPLOAD_FOLDER, str(_tenant_id()))
    os.makedirs(tenant_dir, exist_ok=True)
    path = os.path.join(tenant_dir, unique)
    file.save(path)

    db = _get_tenant_db()
    try:
        doc = Document(
            tenant_id=_tenant_id(),
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

    process_document_task.delay(_tenant_db_name(), doc_id, path, document_type)

    return jsonify({'message': 'Document uploaded', 'document_id': doc_id, 'status': 'processing'}), 201


# ---- Documents CRUD ---------------------------------------------------------
@app.route('/api/history', methods=['GET'])
def get_history():
    """Returns combined history of all single and batch analyses, sorted by date."""
    db = _get_tenant_db()
    try:
        docs = db.query(Document).join(Analysis).filter(Document.tenant_id == _tenant_id()).all()
        batches = db.query(BatchAnalysis).filter(BatchAnalysis.tenant_id == _tenant_id()).all()
        
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
    db = _get_tenant_db()
    try:
        docs = db.query(Document).filter(Document.tenant_id == _tenant_id()).order_by(Document.upload_date.desc()).all()
        return jsonify({'documents': [d.to_dict() for d in docs]})
    finally:
        db.close()


@app.route('/api/documents/<int:doc_id>', methods=['GET'])
def get_document(doc_id):
    db = _get_tenant_db()
    try:
        doc = db.query(Document).filter(Document.id == doc_id, Document.tenant_id == _tenant_id()).first()
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
    db = _get_tenant_db()
    try:
        doc = db.query(Document).filter(Document.id == doc_id, Document.tenant_id == _tenant_id()).first()
        if not doc:
            return jsonify({'error': 'Document not found'}), 404
        try:
            vector_store.remove_document(_tenant_db_name(), _tenant_id(), doc_id)
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
    db = _get_tenant_db()
    try:
        doc = db.query(Document).filter(Document.id == doc_id, Document.tenant_id == _tenant_id()).first()
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
    db = _get_tenant_db()
    try:
        doc = db.query(Document).filter(Document.id == doc_id, Document.tenant_id == _tenant_id()).first()
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

        # Use cached Markdown text if available (avoids re-parsing PDF/DOCX)
        text = doc.markdown_text
        if not text:
            text = extract_text(doc.file_path)
            # Cache for future saves
            doc.markdown_text = text

        chunk_count = vector_store.add_document(_tenant_db_name(), 
            _tenant_id(), doc_id, doc.original_filename, text,
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
    db = _get_tenant_db()
    try:
        doc = db.query(Document).filter(Document.id == doc_id, Document.tenant_id == _tenant_id()).first()
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
    db = _get_tenant_db()
    try:
        doc = db.query(Document).filter(Document.id == doc_id, Document.tenant_id == _tenant_id()).first()
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

        # Get document text (use cache if available)
        text = doc.markdown_text or extract_text(doc.file_path)
        fw_uploaded = framework_store.get_uploaded_frameworks(_tenant_db_name(), _tenant_id())

        try:
            llm = get_llm_client(tenant_id=_tenant_id())
        except RuntimeError as e:
            return jsonify({'error': 'LLM not configured', 'message': str(e)}), 402
        mappings = dict(doc.analysis.framework_mappings or {})

        for key in selected:
            try:
                if fw_uploaded.get(key, False):
                    hits = framework_store.search_framework(_tenant_db_name(), _tenant_id(), key, text[:2000], top_k=8)
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

    # Validate LLM credentials before accepting any files
    try:
        get_llm_client(tenant_id=_tenant_id())
    except RuntimeError as e:
        return jsonify({'error': 'LLM not configured', 'message': str(e)}), 402

    db = _get_tenant_db()
    try:
        documents_info = []
        for file in files:
            if file.filename == '' or not _allowed(file.filename):
                continue

            original = file.filename
            safe = secure_filename(original)
            ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            unique = f"{ts}_{safe}"
            tenant_dir = os.path.join(Config.UPLOAD_FOLDER, str(_tenant_id()))
            os.makedirs(tenant_dir, exist_ok=True)
            path = os.path.join(tenant_dir, unique)
            file.save(path)

            doc = Document(
                tenant_id=_tenant_id(),
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
            tenant_id=_tenant_id(),
            document_type=document_type,
            status='processing',
        )
        db.add(batch)
        db.flush()  # get batch.id before inserting pivot rows

        # Insert pivot rows (one per document)
        for d in documents_info:
            db.add(BatchDocument(batch_id=batch.id, document_id=d['id']))

        db.commit()
        db.refresh(batch)
        batch_id = batch.id

        # Start background processing
        process_batch_task.delay(_tenant_db_name(), batch_id, documents_info, document_type)

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
    db = _get_tenant_db()
    try:
        batch = db.query(BatchAnalysis).filter(BatchAnalysis.id == batch_id, BatchAnalysis.tenant_id == _tenant_id()).first()
        if not batch:
            return jsonify({'error': 'Batch not found'}), 404

        # Get individual document results — single IN query instead of N round-trips
        doc_ids = batch.document_ids
        docs_by_id = {
            doc.id: doc
            for doc in db.query(Document).filter(Document.id.in_(doc_ids)).all()
        }
        individual = []
        for doc_id in doc_ids:
            doc = docs_by_id.get(doc_id)
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
    db = _get_tenant_db()
    try:
        batch = db.query(BatchAnalysis).filter(BatchAnalysis.id == batch_id, BatchAnalysis.tenant_id == _tenant_id()).first()
        if not batch:
            return jsonify({'error': 'Batch not found'}), 404
            
        doc_ids = batch.document_ids
        docs = db.query(Document).filter(Document.id.in_(doc_ids)).all()
        for doc in docs:
            try:
                vector_store.remove_document(_tenant_db_name(), _tenant_id(), doc.id)
            except Exception:
                pass
            if os.path.exists(doc.file_path):
                try:
                    os.remove(doc.file_path)
                except Exception:
                    pass
            db.delete(doc)

        # BatchDocument pivot rows are cascade-deleted with the batch
        db.delete(batch)
        db.commit()
        return jsonify({'message': 'Batch deleted'})
    finally:
        db.close()


@app.route('/api/batch-analysis/<int:batch_id>/save-all', methods=['POST'])
def save_batch_documents(batch_id):
    """Save all documents in a batch to the knowledge base."""
    db = _get_tenant_db()
    try:
        batch = db.query(BatchAnalysis).filter(BatchAnalysis.id == batch_id, BatchAnalysis.tenant_id == _tenant_id()).first()
        if not batch:
            return jsonify({'error': 'Batch not found'}), 404

        body = request.get_json(silent=True) or {}
        preset = body.get('chunk_preset', 'medium')
        chunk_size, overlap = vector_store.CHUNK_PRESETS.get(
            preset, vector_store.CHUNK_PRESETS['medium']
        )

        saved_docs = []
        total_chunks = 0
        docs = db.query(Document).filter(Document.id.in_(batch.document_ids)).all()
        for doc in docs:
            if doc.is_saved:
                continue
            try:
                text = doc.markdown_text or extract_text(doc.file_path)
                chunk_count = vector_store.add_document(_tenant_db_name(), 
                    _tenant_id(), doc.id, doc.original_filename, text,
                    chunk_size=chunk_size, overlap=overlap,
                )
                doc.is_saved = True
                total_chunks += chunk_count
                saved_docs.append(doc.original_filename)
            except Exception as e:
                print(f"Error saving doc {doc.id} to KB: {e}")

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
    db = _get_tenant_db()
    try:
        analyses = (
            db.query(Analysis)
            .join(Document)
            .filter(Document.tenant_id == _tenant_id())
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
    central_db = get_central_db()
    tenant_db = _get_tenant_db()
    try:
        setting = central_db.query(SystemSettings).filter(SystemSettings.key == 'lifetime_tokens').first()
        lifetime_tokens = int(setting.value) if setting else 0
        
        # Calculate total tokens used explicitly in chat
        chat_tokens_query = tenant_db.query(func.sum(ChatHistory.tokens_used)).filter(ChatHistory.tenant_id == _tenant_id()).scalar()
        total_chat_tokens = int(chat_tokens_query) if chat_tokens_query else 0
        
        return jsonify({
            'lifetime_tokens': lifetime_tokens,
            'total_chat_tokens': total_chat_tokens
        })
    finally:
        central_db.close()
        tenant_db.close()

@app.route('/api/stats/reset', methods=['POST'])
def reset_tokens():
    """Reset lifetime and chat token counts to zero."""
    central_db = get_central_db()
    tenant_db = _get_tenant_db()
    try:
        # Reset lifetime settings to 0
        setting = central_db.query(SystemSettings).filter(SystemSettings.key == 'lifetime_tokens').first()
        if setting:
            setting.value = "0"
            
        # Reset all past chat history tokens to 0 to prevent sum rebuilding
        tenant_db.query(ChatHistory).filter(ChatHistory.tenant_id == _tenant_id()).update({ChatHistory.tokens_used: 0})
        
        central_db.commit()
        tenant_db.commit()
        return jsonify({'status': 'success', 'message': 'Token counts reset to 0'})
    except Exception as e:
        central_db.rollback()
        tenant_db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        central_db.close()
        tenant_db.close()


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
        result = chat_file_store.upload_file(_tenant_db_name(), temp_path, file.filename)
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

    db = _get_tenant_db()
    try:
        result = chat_file_store.save_to_kb(_tenant_db_name(), session_id, db, tenant_id=_tenant_id())
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
        chat_file_store.clear_session(_tenant_db_name(), session_id)
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
        # Use the same LLM client as chat (tenant-aware)
        try:
            fill_llm = get_llm_client(tenant_id=_tenant_id())
        except RuntimeError as e:
            return jsonify({'error': 'LLM not configured', 'message': str(e)}), 402

        result = document_filler.fill_document(file_path, filename, fill_llm, db_name=_tenant_db_name(), tenant_id=_tenant_id())

        used_tokens = fill_llm.total_input_tokens + fill_llm.total_output_tokens

        # Track token usage
        _increment_lifetime_tokens(used_tokens)

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
    db = _get_tenant_db()
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
        try:
            chat_llm = get_llm_client(tenant_id=_tenant_id())
        except RuntimeError as e:
            return jsonify({'error': 'LLM not configured', 'message': str(e)}), 402

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
                
                q_hits = vector_store.search(_tenant_db_name(), _tenant_id(), q, top_k=2)
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
            kb_hits = vector_store.search(_tenant_db_name(), _tenant_id(), search_query, top_k=8)
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
        user_msg = ChatHistory(tenant_id=_tenant_id(), document_id=None, role='user', message=message)
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

Output your response STRICTLY as a JSON object with the following structure:
{{
  "answer": "<Your detailed answer with citations>",
  "confidence_score": <An integer from 1 to 100 representing how confident you are in this answer based exclusively on the KB context>
}}

Return ONLY the JSON object, with no markdown formatting or extra text."""
        else:
            prompt = knowledge_chat_prompt(hits, history_dicts, message)
        raw_answer = chat_llm.invoke(prompt)

        # --- Parse JSON response ---
        import json
        
        answer_text = raw_answer
        confidence_score = None
        try:
            # Clean up if enclosed in markdown
            cleaned = raw_answer.strip()
            if "```json" in cleaned:
                cleaned = cleaned.split("```json")[1].split("```")[0].strip()
            elif "```" in cleaned:
                cleaned = cleaned.split("```")[1].split("```")[0].strip()
            
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict) and "answer" in parsed:
                answer_text = parsed["answer"]
                confidence_score = parsed.get("confidence_score")
        except Exception as e:
            print(f"Failed to parse LLM JSON response: {e}")
            # Fallback to returning raw text without confidence score
            pass
        
        # Calculate total tokens used across the rewrite + final answer
        used_tokens = chat_llm.total_input_tokens + chat_llm.total_output_tokens

        # Save assistant message
        assistant_msg = ChatHistory(tenant_id=_tenant_id(), document_id=None, role='assistant', message=answer_text, tokens_used=used_tokens)
        db.add(assistant_msg)
        db.commit()
        
        # Update global counter
        _increment_lifetime_tokens(used_tokens)

        # Extract unique source docs for citation metadata
        cited_docs = {}
        for h in hits:
            if h.get('filename'):
                cited_docs[h['filename']] = True

        return jsonify({
            'answer': answer_text,
            'citations': list(cited_docs.keys()),
            'tokens_used': used_tokens,
            'confidence_score': confidence_score,
            'has_uploaded_file': bool(session_id),
            'session_id': session_id,
        })

    except Exception as e:
        print(f"Chat error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()



def process_batch_questions(filename: str, user_message: str):
    """Extract questions from a file and answer them one by one."""
    import json
    from agents.prompts import extract_questions_prompt, batch_question_answer_prompt

    # 1. Retrieve full text of the file
    print(f"Retrieving text for {filename}...")
    full_text = vector_store.get_document_text(_tenant_db_name(), _tenant_id(), filename)
        
    # helper to save history
    def save_and_return(answer_text, citations_list, used_tokens=0):
        # Save interaction to DB
        db = _get_tenant_db()
        try:
            # User message
            db.add(ChatHistory(
                tenant_id=_tenant_id(),
                role='user',
                message=user_message,
                document_id=None
            ))
            # Assistant message
            db.add(ChatHistory(
                tenant_id=_tenant_id(),
                role='assistant',
                message=answer_text,
                document_id=None,
                tokens_used=used_tokens
            ))
            db.commit()
            
            # Update global counter
            if used_tokens > 0:
                _increment_lifetime_tokens(used_tokens)
        finally:
            db.close()
            
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
        hits = vector_store.search(_tenant_db_name(), _tenant_id(), q, top_k=10, filters={"filename_ne": filename})
        
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
    db = _get_tenant_db()
    try:
        msgs = (
            db.query(ChatHistory)
            .filter(ChatHistory.document_id.is_(None), ChatHistory.tenant_id == _tenant_id())
            .order_by(ChatHistory.timestamp.asc())
            .all()
        )
        return jsonify({'messages': [m.to_dict() for m in msgs]})
    finally:
        db.close()


@app.route('/api/chat/history', methods=['DELETE'])
def clear_chat_history():
    """Clear global chat history."""
    db = _get_tenant_db()
    try:
        db.query(ChatHistory).filter(
            ChatHistory.document_id.is_(None),
            ChatHistory.tenant_id == _tenant_id()
        ).delete()
        db.commit()
        return jsonify({'message': 'Chat history cleared'})
    finally:
        db.close()


@app.route('/api/kb/stats', methods=['GET'])
def kb_stats():
    """Return knowledge base statistics."""
    stats = vector_store.get_stats(_tenant_db_name(), _tenant_id())
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


def _reindex_worker(worker_tenant_id: int, worker_db_name: str):
    """Background worker: re-extract and re-index all saved documents for a tenant."""
    _reindex_cancel.clear()
    db = get_tenant_session(worker_db_name)
    try:
        docs = db.query(Document).filter(
            Document.is_saved == True,
            Document.tenant_id == worker_tenant_id
        ).all()
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
                text = doc.markdown_text or extract_text(doc.file_path)
                vector_store.add_document(worker_db_name, worker_tenant_id, doc.id, doc.original_filename, text)
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
    tid = _tenant_id()
    db_name = _tenant_db_name()
    t = threading.Thread(target=_reindex_worker, args=(tid, db_name), daemon=True)
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
    db = _get_tenant_db()
    try:
        standards = db.query(FrameworkStandard).filter(
            FrameworkStandard.tenant_id == _tenant_id()
        ).order_by(
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
    return jsonify(framework_store.get_uploaded_frameworks(_tenant_db_name(), _tenant_id()))


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
    # Store in sub-directory per tenant + framework key
    fw_dir = os.path.join(Config.UPLOAD_FOLDER, str(_tenant_id()), 'frameworks', fw_key)
    os.makedirs(fw_dir, exist_ok=True)
    file_path = os.path.join(fw_dir, f"{version}_{filename}")
    file.save(file_path)

    # Extract text and index into vector store
    try:
        text = extract_text(file_path)
        chunk_count = framework_store.add_framework(_tenant_db_name(), _tenant_id(), fw_key, version, filename, text)
    except Exception as e:
        return jsonify({'error': f'Failed to process file: {e}'}), 500

    # Save to DB
    db = _get_tenant_db()
    try:
        record = FrameworkStandard(
            tenant_id=_tenant_id(),
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
    db = _get_tenant_db()
    try:
        record = db.query(FrameworkStandard).filter(
            FrameworkStandard.id == fw_id,
            FrameworkStandard.tenant_id == _tenant_id()
        ).first()
        if not record:
            return jsonify({'error': 'Framework not found'}), 404

        # Remove from vector store
        framework_store.remove_framework(_tenant_db_name(), _tenant_id(), record.framework_key, record.version, record.filename)

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


# ---- Admin: Tenant Management ------------------------------------------------
# All routes below require is_admin = True on the caller's AuthorizedApp.

@app.route('/api/admin/tenants', methods=['GET'])
def admin_list_tenants():
    """[Admin] List all tenants."""
    err = _require_admin()
    if err:
        return err
    db = get_central_db()
    try:
        tenants = db.query(Tenant).order_by(Tenant.created_at.desc()).all()
        return jsonify({'tenants': [t.to_dict() for t in tenants]})
    finally:
        db.close()


@app.route('/api/admin/tenants', methods=['POST'])
def admin_create_tenant():
    """[Admin] Create a new tenant and return its first API key."""
    err = _require_admin()
    if err:
        return err
    data = request.get_json(force=True) or {}
    name = data.get('name', '').strip()
    slug = data.get('slug', '').strip().lower().replace(' ', '-')
    if not name or not slug:
        return jsonify({'error': 'name and slug are required'}), 400

    db = get_central_db()
    try:
        existing = db.query(Tenant).filter_by(slug=slug).first()
        if existing:
            return jsonify({'error': f'Slug "{slug}" is already taken'}), 409

        tenant = Tenant(name=name, slug=slug, is_active=True)
        db.add(tenant)
        db.flush()

        # Create a dedicated database for this tenant
        tenant_db_name = create_tenant_database(slug)
        tenant.db_name = tenant_db_name

        # Auto-create a first API key for this tenant
        first_key = f"sk-{uuid.uuid4()}"
        app_entry = AuthorizedApp(
            tenant_id=tenant.id,
            name=f"{name} — Default Key",
            api_key_hash=hash_token(first_key),
            api_key_prefix=first_key[:10],
            is_active=True,
            is_admin=False,
        )
        db.add(app_entry)
        db.commit()
        db.refresh(tenant)
        return jsonify({
            'tenant': tenant.to_dict(),
            'first_api_key': first_key,
        }), 201
    finally:
        db.close()


@app.route('/api/admin/tenants/<int:tenant_id>', methods=['PATCH'])
def admin_update_tenant(tenant_id):
    """[Admin] Enable or disable a tenant."""
    err = _require_admin()
    if err:
        return err
    db = get_central_db()
    try:
        tenant = db.query(Tenant).filter_by(id=tenant_id).first()
        if not tenant:
            return jsonify({'error': 'Tenant not found'}), 404
        data = request.get_json(force=True) or {}
        if 'is_active' in data:
            tenant.is_active = bool(data['is_active'])
        if 'name' in data:
            tenant.name = data['name'].strip()
        db.commit()
        return jsonify(tenant.to_dict())
    finally:
        db.close()


@app.route('/api/admin/tenants/<int:tenant_id>', methods=['DELETE'])
def admin_delete_tenant(tenant_id):
    """[Admin] Delete a tenant and all its authorized apps."""
    err = _require_admin()
    if err:
        return err
    db = get_central_db()
    try:
        tenant = db.query(Tenant).filter_by(id=tenant_id).first()
        if not tenant:
            return jsonify({'error': 'Tenant not found'}), 404
        tenant_name = tenant.name
        db.delete(tenant)
        db.commit()
        return jsonify({'message': f'Tenant {tenant_name} deleted successfully', 'id': tenant_id})
    finally:
        db.close()


@app.route('/api/admin/tenants/<int:tenant_id>/keys', methods=['GET'])
def admin_list_tenant_keys(tenant_id):
    """[Admin] List all API keys for a tenant."""
    err = _require_admin()
    if err:
        return err
    db = get_central_db()
    try:
        apps = db.query(AuthorizedApp).filter(
            AuthorizedApp.tenant_id == tenant_id
        ).order_by(AuthorizedApp.created_at.desc()).all()
        return jsonify({'keys': [a.to_dict() for a in apps]})
    finally:
        db.close()


@app.route('/api/admin/tenants/<int:tenant_id>/keys', methods=['POST'])
def admin_create_tenant_key(tenant_id):
    """[Admin] Create a new API key for a tenant."""
    err = _require_admin()
    if err:
        return err
    db = get_central_db()
    try:
        tenant = db.query(Tenant).filter_by(id=tenant_id).first()
        if not tenant:
            return jsonify({'error': 'Tenant not found'}), 404
        data = request.get_json(force=True) or {}
        name = data.get('name', 'API Key').strip()
        is_admin_key = bool(data.get('is_admin', False))
        new_key = f"sk-{uuid.uuid4()}"
        app_entry = AuthorizedApp(
            tenant_id=tenant_id,
            name=name,
            api_key_hash=hash_token(new_key),
            api_key_prefix=new_key[:10],
            is_active=True,
            is_admin=is_admin_key,
        )
        db.add(app_entry)
        db.commit()
        db.refresh(app_entry)
        # Return the full key ONCE in the creation response
        result = app_entry.to_dict()
        result['api_key'] = new_key
        return jsonify(result), 201
    finally:
        db.close()


@app.route('/api/admin/tenants/<int:tenant_id>/keys/<int:key_id>', methods=['DELETE'])
def admin_revoke_tenant_key(tenant_id, key_id):
    """[Admin] Revoke a specific API key for a tenant."""
    err = _require_admin()
    if err:
        return err
    db = get_central_db()
    try:
        app_entry = db.query(AuthorizedApp).filter(
            AuthorizedApp.id == key_id, AuthorizedApp.tenant_id == tenant_id
        ).first()
        if not app_entry:
            return jsonify({'error': 'Key not found'}), 404
        db.delete(app_entry)
        db.commit()
        return jsonify({'message': f'Key "{app_entry.name}" revoked.'})
    finally:
        db.close()


# ==============================================================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=Config.DEBUG)


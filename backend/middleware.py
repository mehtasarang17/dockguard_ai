"""
Authentication middleware for DocGuard AI.

Enforces API key auth on all /api/ routes, checks key expiration,
populates g.tenant_id, g.is_admin, and g.tenant_db_name per request.
"""
import json
from datetime import datetime
from flask import request, jsonify, g
from config import Config
from models import AuthorizedApp, Tenant, get_central_db
from crypto import hash_token


# Routes that do NOT require any authentication
AUTH_EXEMPT_ROUTES = {'health', 'provision', 'provision_refresh_key', 'verify_api_key_info'}

# Swagger resources are public (no key needed to view docs)
SWAGGER_PREFIXES = ('/apispec.json',)

# Cached default tenant db_name (populated on first request)
_default_tenant_db_name = None


def _resolve_default_tenant():
    """Look up the first active tenant (cached for the process lifetime)."""
    global _default_tenant_db_name

    db = get_central_db()
    try:
        # Dynamically find the first active tenant, NOT hardcoded id=1
        tenant = db.query(Tenant).filter_by(is_active=True).order_by(Tenant.id).first()
        if tenant and tenant.db_name:
            _default_tenant_db_name = tenant.db_name
            return tenant
        # Fallback: use the central database name
        _default_tenant_db_name = Config.DATABASE_URL.rsplit('/', 1)[1]
        return None
    finally:
        db.close()


def register_middleware(app):
    """Attach the before_request auth middleware to the Flask app."""

    @app.before_request
    def require_api_key():
        """Enforce API key auth on all /api/ routes; populate g.tenant_id, g.is_admin & g.tenant_db_name."""
        # Allow Swagger UI and spec to load without a key
        if any(request.path.startswith(p) for p in SWAGGER_PREFIXES):
            return None
        if not request.path.startswith('/api/'):
            return None
        if request.endpoint in AUTH_EXEMPT_ROUTES:
            return None

        # First: check for an explicit API key (X-API-Key header or Authorization Bearer).
        # This MUST take priority over X-Internal-Token so that tenant-scoped keys work
        # correctly even when Nginx injects the internal token on all proxied requests.
        api_key = request.headers.get('X-API-Key', '')
        auth_header = request.headers.get('Authorization', '')

        # Track whether the caller is explicitly trying to authenticate.
        # If they are, we must NOT silently fall back to X-Internal-Token.
        explicit_auth_attempt = bool(api_key) or bool(auth_header)

        if not api_key and auth_header.startswith('Bearer '):
            api_key = auth_header[7:].strip()

        if api_key:
            db = get_central_db()
            try:
                app_entry = db.query(AuthorizedApp).filter_by(api_key_hash=hash_token(api_key)).first()
                if not app_entry:
                    return jsonify({'error': 'Invalid API key', 'message': 'API key not recognised.'}), 401
                if not app_entry.is_active:
                    return jsonify({
                        'error': 'Application disabled',
                        'message': 'This application has been disabled.',
                    }), 403
                # Check expiration
                if app_entry.expires_at and app_entry.expires_at < datetime.utcnow():
                    return jsonify({
                        'error': 'API key expired',
                        'message': 'Your API key has expired. Use your refresh token at POST /api/refresh-key to generate a new one.',
                        'expired_at': app_entry.expires_at.isoformat(),
                    }), 401
                # Record last-used timestamp
                app_entry.last_used = datetime.utcnow()
                db.commit()
                g.tenant_id = app_entry.tenant_id
                g.is_admin = bool(app_entry.is_admin)

                # Resolve the tenant's database name
                tenant = db.query(Tenant).filter_by(id=app_entry.tenant_id).first()
                if tenant and tenant.db_name:
                    g.tenant_db_name = tenant.db_name
                else:
                    # Fallback: use the central database
                    g.tenant_db_name = Config.DATABASE_URL.rsplit('/', 1)[1]

                return None
            finally:
                db.close()

        # If an explicit auth was attempted but the key was invalid/empty, reject immediately.
        # Do NOT fall through to internal token — that would let Swagger bypass auth.
        if explicit_auth_attempt:
            return jsonify({
                'error': 'Invalid API key',
                'message': 'Provide a valid key as: Authorization: Bearer sk-your-key',
            }), 401

        # Fallback: frontend proxy via Nginx — assign to default tenant with admin rights.
        # Only reached when NO explicit auth headers are present (i.e. the main browser UI).
        internal_token = request.headers.get('X-Internal-Token', '')
        if internal_token and internal_token == Config.INTERNAL_TOKEN:
            default_tenant = _resolve_default_tenant()
            g.tenant_id = default_tenant.id if default_tenant else 1
            g.is_admin = True
            g._internal_token_auth = True
            g.tenant_db_name = default_tenant.db_name if default_tenant else Config.DATABASE_URL.rsplit('/', 1)[1]
            return None

        return jsonify({
            'error': 'Authentication required',
            'message': 'Please provide an API key via X-API-Key or Authorization: Bearer.',
        }), 401

    @app.after_request
    def wrap_api_response(response):
        """Wrap JSON responses for external API-key callers.

        GET  → {"success": true/false, "data": { ... }}
        POST → {"success": true/false, "message": { ... }}

        Internal frontend requests (X-Internal-Token) are NOT wrapped
        so the existing UI continues to work without changes.
        """
        # Only wrap /api/ JSON responses
        if not request.path.startswith('/api/'):
            return response
        if not response.content_type or 'application/json' not in response.content_type:
            return response
        # Skip wrapping for internal frontend requests
        if getattr(g, '_internal_token_auth', False):
            return response

        data = response.get_json(silent=True)
        if data is None:
            return response
        # Don't double-wrap if already in envelope
        if isinstance(data, dict) and 'success' in data:
            return response

        is_success = response.status_code < 400
        key = 'message' if request.method == 'GET' else 'data'
        wrapped = {'success': is_success, key: data}

        response.set_data(json.dumps(wrapped))
        return response

"""
LLM Factory — returns the correct client (Bedrock or Ollama) based on
the active provider stored in system_settings.

Client pooling: one singleton per provider is reused across requests.
Tenant-aware: per-tenant LLM clients are cached with a TTL and use
the tenant's own AWS Bedrock credentials if configured.

Token tracking: each analysis creates its own TokenTracker to avoid
shared-state corruption across concurrent requests.
"""
import threading
import time
from typing import Optional

from agents.bedrock_client import BedrockClient
from agents.ollama_client import OllamaClient
from models import SessionLocal, SystemSettings, Tenant
from crypto import decrypt_value


# ---------------------------------------------------------------------------
#  Token Tracker — per-analysis token counter (thread-safe)
# ---------------------------------------------------------------------------

class TokenTracker:
    """Lightweight, per-analysis token counter.

    Create one of these for each analysis run and pass it through the pipeline.
    Thread-safe so parallel agents can record tokens concurrently.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.input_tokens = 0
        self.output_tokens = 0

    def record(self, input_tokens: int = 0, output_tokens: int = 0):
        with self._lock:
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens

    def reset(self):
        with self._lock:
            self.input_tokens = 0
            self.output_tokens = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# ---------------------------------------------------------------------------
#  Provider helpers
# ---------------------------------------------------------------------------

def get_active_provider() -> str:
    """Read the active LLM provider from the DB. Defaults to 'bedrock'."""
    try:
        db = SessionLocal()
        row = db.query(SystemSettings).filter_by(key='llm_provider').first()
        db.close()
        if row:
            return row.value
    except Exception:
        pass
    return 'bedrock'


def set_active_provider(provider: str):
    """Persist the chosen LLM provider ('bedrock' or 'ollama')."""
    db = SessionLocal()
    try:
        row = db.query(SystemSettings).filter_by(key='llm_provider').first()
        if row:
            row.value = provider
        else:
            db.add(SystemSettings(key='llm_provider', value=provider))
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
#  Singleton client pool (thread-safe)
# ---------------------------------------------------------------------------
_client_lock = threading.Lock()
_clients: dict = {}  # provider_name -> client instance

# ---------------------------------------------------------------------------
#  Tenant-aware client pool (thread-safe, with TTL)
# ---------------------------------------------------------------------------
_tenant_client_lock = threading.Lock()
_tenant_clients: dict = {}  # "tenant_{id}" -> (client, created_at)
TENANT_CLIENT_TTL = 300  # 5 minutes


def _get_tenant_llm_config(tenant_id: int) -> Optional[dict]:
    """Fetch and decrypt LLM config for a tenant.

    Returns a dict with keys: bearer_token, region, model_id, is_default
    or None if the tenant does not exist.
    """
    try:
        db = SessionLocal()
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        db.close()

        if not tenant:
            return None

        return {
            'bearer_token': decrypt_value(tenant.llm_aws_bearer_token) if tenant.llm_aws_bearer_token else None,
            'region': tenant.llm_aws_region,
            'model_id': tenant.llm_bedrock_model_id,
            'is_default': tenant.slug == 'default',
        }
    except Exception as e:
        print(f"⚠️  Failed to fetch tenant {tenant_id} LLM config: {e}")
        return None


def clear_tenant_llm_cache(tenant_id: int):
    """Clear cached LLM client for a tenant after config update."""
    cache_key = f"tenant_{tenant_id}"
    with _tenant_client_lock:
        if cache_key in _tenant_clients:
            del _tenant_clients[cache_key]
            print(f"🗑️  Cleared LLM client cache for tenant {tenant_id}")


def _get_global_client():
    """Get the global (non-tenant-specific) LLM client."""
    provider = get_active_provider()
    with _client_lock:
        if provider not in _clients:
            if provider == 'ollama':
                _clients[provider] = OllamaClient()
            else:
                _clients[provider] = BedrockClient()
            print(f"🔗 LLM client pool: created {provider} singleton")
    return _clients[provider]


def get_llm_client(tenant_id: int = None):
    """Factory: return the correct LLM client.

    If tenant_id is provided and the tenant has LLM config, returns
    a tenant-specific Bedrock client. Otherwise falls back to global.

    Returns a **shared singleton** — do NOT store per-request state on it.
    Use TokenTracker for per-analysis token counting.

    Both clients share the same public API:
      - invoke(prompt, max_tokens, temperature, model_override) -> (str, dict)
      - invoke_fast(prompt, max_tokens, temperature) -> (str, dict)
      - parse_json(text)  [staticmethod]
    """
    # If no tenant_id, use global client
    if tenant_id is None:
        return _get_global_client()

    cache_key = f"tenant_{tenant_id}"

    with _tenant_client_lock:
        # Check cache
        if cache_key in _tenant_clients:
            client, created_at = _tenant_clients[cache_key]
            # Check TTL
            if time.time() - created_at < TENANT_CLIENT_TTL:
                return client
            else:
                # Expired, remove from cache
                del _tenant_clients[cache_key]

    # Fetch tenant config
    config = _get_tenant_llm_config(tenant_id)

    if config and config.get('bearer_token'):
        pass  # fall through to create tenant-specific client below
    elif config and config.get('is_default'):
        # Default tenant: permitted to use global credentials
        return _get_global_client()
    else:
        # Provisioned tenant with no LLM credentials — do not leak global creds
        raise RuntimeError(
            f"No LLM credentials configured for tenant {tenant_id}. "
            "Please set AWS Bedrock credentials via the provisioning API or admin settings."
        )

    # Create tenant-specific client
    client = BedrockClient(
        bearer_token=config['bearer_token'],
        region=config['region'],
        model_id=config['model_id'],
    )

    # Cache it
    with _tenant_client_lock:
        _tenant_clients[cache_key] = (client, time.time())
        print(f"🔗 LLM client pool: created tenant {tenant_id} client (region={config['region']})")

    return client


def get_token_tracker() -> TokenTracker:
    """Create a fresh TokenTracker for a new analysis run."""
    return TokenTracker()

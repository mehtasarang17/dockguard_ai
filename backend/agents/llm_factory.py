"""
LLM Factory — returns the correct client (Bedrock or Ollama) based on
the active provider stored in system_settings.

Client pooling: one singleton per provider is reused across requests.
Token tracking: each analysis creates its own TokenTracker to avoid
shared-state corruption across concurrent requests.
"""
import threading
from agents.bedrock_client import BedrockClient
from agents.ollama_client import OllamaClient
from models import SessionLocal, SystemSettings


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


def get_llm_client():
    """Factory: return the correct LLM client based on active provider.

    Returns a **shared singleton** — do NOT store per-request state on it.
    Use TokenTracker for per-analysis token counting.

    Both clients share the same public API:
      - invoke(prompt, max_tokens, temperature, model_override) -> (str, dict)
      - invoke_fast(prompt, max_tokens, temperature) -> (str, dict)
      - parse_json(text)  [staticmethod]
    """
    provider = get_active_provider()
    with _client_lock:
        if provider not in _clients:
            if provider == 'ollama':
                _clients[provider] = OllamaClient()
            else:
                _clients[provider] = BedrockClient()
            print(f"🔗 LLM client pool: created {provider} singleton")
    return _clients[provider]


def get_token_tracker() -> TokenTracker:
    """Create a fresh TokenTracker for a new analysis run."""
    return TokenTracker()

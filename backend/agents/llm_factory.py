"""
LLM Factory — returns the correct client (Bedrock or Ollama) based on
the active provider stored in system_settings.
"""
from agents.bedrock_client import BedrockClient
from agents.ollama_client import OllamaClient
from models import SessionLocal, SystemSettings


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


def get_llm_client():
    """Factory: return the correct LLM client based on active provider.

    Both clients share the same public API:
      - invoke(prompt, max_tokens, temperature, model_override)
      - invoke_fast(prompt, max_tokens, temperature)
      - parse_json(text)  [staticmethod]
      - reset_tokens()
      - total_input_tokens / total_output_tokens  [int attributes]
    """
    provider = get_active_provider()
    if provider == 'ollama':
        return OllamaClient()
    return BedrockClient()

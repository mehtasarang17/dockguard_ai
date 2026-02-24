"""
Ollama LLM client — drop-in alternative to BedrockClient.

Uses the Ollama REST API (/api/chat) for local model inference.
Implements the same public interface: invoke(), invoke_fast(), parse_json(),
reset_tokens(), and token tracking attributes.
"""
import json
import requests
from config import Config


class OllamaClient:
    """Local LLM client using Ollama REST API."""

    def __init__(self, model: str = None):
        self.base_url = Config.OLLAMA_BASE_URL.rstrip('/')
        self.model = model or Config.OLLAMA_MODEL
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    # ----- Public API -----------------------------------------------------------
    def reset_tokens(self):
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def invoke(self, prompt: str, max_tokens: int = 4096, temperature: float = 0.3,
               model_override: str = None) -> str:
        """Send a prompt to the Ollama model and return the text response."""
        model = model_override or self.model
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        resp = requests.post(url, json=payload, timeout=300)
        if not resp.ok:
            print(f"Ollama error {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()

        # Track tokens (Ollama provides these in the response)
        if 'prompt_eval_count' in data:
            self.total_input_tokens += data['prompt_eval_count']
        if 'eval_count' in data:
            self.total_output_tokens += data['eval_count']

        return data.get('message', {}).get('content', '')

    def invoke_fast(self, prompt: str, max_tokens: int = 4096, temperature: float = 0.2) -> str:
        """For Ollama, fast model is the same as the primary model."""
        return self.invoke(prompt, max_tokens=max_tokens, temperature=temperature)

    # ----- JSON parsing (shared with BedrockClient) ----------------------------
    @staticmethod
    def parse_json(text: str) -> dict:
        """Parse JSON from model output, handling markdown fences etc."""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # try code fences
        for fence in ('```json', '```'):
            if fence in text:
                block = text.split(fence, 1)[1]
                block = block.split('```', 1)[0].strip()
                try:
                    return json.loads(block)
                except json.JSONDecodeError:
                    pass
        # brute-force find first { … }
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Could not parse JSON from LLM response:\n{text[:500]}")

    # ----- Health check --------------------------------------------------------
    @classmethod
    def is_available(cls) -> bool:
        """Check if the Ollama server is reachable."""
        try:
            resp = requests.get(
                f"{Config.OLLAMA_BASE_URL.rstrip('/')}/api/tags",
                timeout=5,
            )
            return resp.ok
        except Exception:
            return False

    @classmethod
    def list_models(cls) -> list:
        """Return list of locally available model names."""
        try:
            resp = requests.get(
                f"{Config.OLLAMA_BASE_URL.rstrip('/')}/api/tags",
                timeout=5,
            )
            if resp.ok:
                return [m['name'] for m in resp.json().get('models', [])]
        except Exception:
            pass
        return []

    @classmethod
    def pull_model_stream(cls, model: str = None):
        """Pull a model with streaming progress. Yields JSON-encoded progress dicts."""
        model = model or Config.OLLAMA_MODEL
        try:
            resp = requests.post(
                f"{Config.OLLAMA_BASE_URL.rstrip('/')}/api/pull",
                json={"name": model, "stream": True},
                stream=True,
                timeout=600,
            )
            resp.raise_for_status()
            for line in resp.iter_lines():
                if line:
                    try:
                        data = json.loads(line)
                        yield data
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            yield {"status": "error", "error": str(e)}

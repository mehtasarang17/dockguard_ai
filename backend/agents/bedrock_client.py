import json
import boto3
import requests
from config import Config


class BedrockClient:
    """AWS Bedrock client for Amazon Nova models via the Converse API.

    Supports two auth methods:
    - Bearer token (via AWS_BEARER_TOKEN_BEDROCK or passed directly)
    - IAM credentials (access key / secret key / session token)

    Supports hybrid model routing: a primary model and a fast model.
    Use invoke() for the default model, invoke_fast() for the cheaper/faster model.

    This client is designed to be used as a **singleton** (via llm_factory).
    Token tracking is handled externally via TokenTracker — do NOT store
    per-request state on this instance.

    For per-tenant configuration, pass bearer_token, region, and model_id
    to the constructor to override global Config values.
    """

    def __init__(self, bearer_token: str = None, region: str = None, model_id: str = None):
        """Initialize the Bedrock client.

        Args:
            bearer_token: Optional per-tenant bearer token (overrides Config)
            region: Optional AWS region (overrides Config.AWS_REGION)
            model_id: Optional model ID (overrides Config.BEDROCK_MODEL_ID)
        """
        # Use passed values or fall back to global config
        self.region = region or Config.AWS_REGION
        self.model_id = model_id or Config.BEDROCK_MODEL_ID
        self.model_id_fast = Config.BEDROCK_MODEL_ID_FAST

        # Legacy per-instance counters (kept for backward compat, e.g. chat)
        self.total_input_tokens = 0
        self.total_output_tokens = 0

        # Determine auth method: prefer passed bearer token, then global config
        effective_bearer_token = bearer_token or Config.AWS_BEARER_TOKEN_BEDROCK

        if effective_bearer_token:
            self.client = boto3.client(
                'bedrock-runtime',
                region_name=self.region,
                aws_access_key_id='',
                aws_secret_access_key='',
            )
            self.bearer_token = effective_bearer_token
            self.use_bearer = True
        else:
            kwargs = {
                'service_name': 'bedrock-runtime',
                'region_name': self.region,
            }
            if Config.AWS_ACCESS_KEY_ID and Config.AWS_SECRET_ACCESS_KEY:
                kwargs['aws_access_key_id'] = Config.AWS_ACCESS_KEY_ID
                kwargs['aws_secret_access_key'] = Config.AWS_SECRET_ACCESS_KEY
            if Config.AWS_SESSION_TOKEN:
                kwargs['aws_session_token'] = Config.AWS_SESSION_TOKEN

            self.client = boto3.client(**kwargs)
            self.use_bearer = False

    # ----- Public API -----------------------------------------------------------
    def reset_tokens(self):
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def invoke(self, prompt: str, max_tokens: int = 4096, temperature: float = 0.3,
               model_override: str = None, tracker=None) -> str:
        """Send a prompt to Amazon Nova and return the text response.

        Args:
            prompt: The prompt text.
            max_tokens: Max tokens in response.
            temperature: Sampling temperature (lower = more deterministic).
            model_override: If set, use this model ID instead of the default.
            tracker: Optional TokenTracker for per-analysis accounting.
        """
        model_id = model_override or self.model_id
        if self.use_bearer:
            return self._invoke_bearer(prompt, max_tokens, temperature, model_id, tracker)
        return self._invoke_converse(prompt, max_tokens, temperature, model_id, tracker)

    def invoke_fast(self, prompt: str, max_tokens: int = 4096, temperature: float = 0.2,
                    tracker=None) -> str:
        """Send a prompt using the fast/cheap model.

        Falls back to the primary model if the fast model is unavailable
        (e.g. not enabled in the current AWS region).
        """
        if self.model_id_fast and self.model_id_fast != self.model_id:
            try:
                return self.invoke(prompt, max_tokens=max_tokens, temperature=temperature,
                                   model_override=self.model_id_fast, tracker=tracker)
            except Exception as e:
                if not getattr(self, '_fast_fallback_warned', False):
                    print(f"⚠️  Fast model ({self.model_id_fast}) unavailable, falling back to primary model. Error: {e}")
                    self._fast_fallback_warned = True
        # Fall back to primary model
        return self.invoke(prompt, max_tokens=max_tokens, temperature=temperature, tracker=tracker)

    # ----- Private helpers ------------------------------------------------------
    def _invoke_converse(self, prompt: str, max_tokens: int, temperature: float,
                         model_id: str, tracker=None) -> str:
        """Call Bedrock Converse API (works with Amazon Nova and other supported models)."""
        resp = self.client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
        if 'usage' in resp:
            inp = resp['usage'].get('inputTokens', 0)
            out = resp['usage'].get('outputTokens', 0)
            self.total_input_tokens += inp
            self.total_output_tokens += out
            if tracker:
                tracker.record(input_tokens=inp, output_tokens=out)
        return resp['output']['message']['content'][0]['text']

    def _invoke_bearer(self, prompt: str, max_tokens: int, temperature: float,
                       model_id: str, tracker=None) -> str:
        """Call Bedrock Converse API via Bearer token auth (HTTP)."""
        url = (
            f"https://bedrock-runtime.{self.region}.amazonaws.com"
            f"/model/{model_id}/converse"
        )
        headers = {
            'Authorization': f'Bearer {self.bearer_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        payload = {
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        if not resp.ok:
            print(f"Bedrock Bearer error {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()

        if 'usage' in data:
            inp = data['usage'].get('inputTokens', 0)
            out = data['usage'].get('outputTokens', 0)
            self.total_input_tokens += inp
            self.total_output_tokens += out
            if tracker:
                tracker.record(input_tokens=inp, output_tokens=out)

        return data['output']['message']['content'][0]['text']

    # ----- JSON parsing ---------------------------------------------------------
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
        # Attempt to repair truncated JSON (close open brackets/braces)
        if start != -1:
            fragment = text[start:]
            # Strip trailing incomplete string/value
            for ch in (',', '"', "'"):
                idx = fragment.rfind(ch)
                if idx > 0:
                    candidate = fragment[:idx]
                    # Close any open [ and {
                    open_b = candidate.count('[') - candidate.count(']')
                    open_c = candidate.count('{') - candidate.count('}')
                    candidate += ']' * max(open_b, 0) + '}' * max(open_c, 0)
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        continue
        raise ValueError(f"Could not parse JSON from LLM response:\n{text[:500]}")

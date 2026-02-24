import json
import boto3
import requests
from config import Config


class BedrockClient:
    """AWS Bedrock client supporting Claude and Nova models with multiple auth methods.
    
    Supports hybrid model routing: a primary model and a fast model.
    Use invoke() for the default model, invoke_fast() for the cheaper/faster model.
    """

    def __init__(self):
        self.model_id = Config.BEDROCK_MODEL_ID
        self.model_id_fast = Config.BEDROCK_MODEL_ID_FAST
        self.is_nova = 'nova' in self.model_id.lower()
        self.is_claude = 'claude' in self.model_id.lower()
        self.total_input_tokens = 0
        self.total_output_tokens = 0

        if Config.AWS_BEARER_TOKEN_BEDROCK:
            self.client = boto3.client(
                'bedrock-runtime',
                region_name=Config.AWS_REGION,
                aws_access_key_id='',
                aws_secret_access_key='',
            )
            self.bearer_token = Config.AWS_BEARER_TOKEN_BEDROCK
            self.use_bearer = True
        else:
            kwargs = {
                'service_name': 'bedrock-runtime',
                'region_name': Config.AWS_REGION,
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
               model_override: str = None) -> str:
        """Send a prompt to the model and return the text response.
        
        Args:
            prompt: The prompt text.
            max_tokens: Max tokens in response.
            temperature: Sampling temperature (lower = more deterministic).
            model_override: If set, use this model ID instead of the default.
        """
        model_id = model_override or self.model_id
        if self.use_bearer:
            return self._invoke_bearer(prompt, max_tokens, temperature, model_id)
        # Check if this specific model ID is Claude-style
        if 'claude' in model_id.lower():
            return self._invoke_claude(prompt, max_tokens, temperature, model_id)
        return self._invoke_converse(prompt, max_tokens, temperature, model_id)

    def invoke_fast(self, prompt: str, max_tokens: int = 4096, temperature: float = 0.2) -> str:
        """Send a prompt using the fast/cheap model (Haiku by default).
        
        Falls back to the primary model if the fast model is unavailable
        (e.g. not enabled in the current AWS region).
        """
        if self.model_id_fast and self.model_id_fast != self.model_id:
            try:
                return self.invoke(prompt, max_tokens=max_tokens, temperature=temperature,
                                   model_override=self.model_id_fast)
            except Exception as e:
                if not getattr(self, '_fast_fallback_warned', False):
                    print(f"⚠️  Fast model ({self.model_id_fast}) unavailable, falling back to primary model. Error: {e}")
                    self._fast_fallback_warned = True
        # Fall back to primary model
        return self.invoke(prompt, max_tokens=max_tokens, temperature=temperature)

    # ----- Private helpers ------------------------------------------------------
    def _invoke_claude(self, prompt: str, max_tokens: int, temperature: float,
                       model_id: str) -> str:
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        })
        resp = self.client.invoke_model(modelId=model_id, body=body)
        data = json.loads(resp['body'].read())
        
        # Claude text completions API might return token usage but it varies. 
        # Generally AWS wrappers provide them in response metadata, but sometimes Anthropic 
        # returns it directly in the data. If present, add to counts.
        if 'usage' in data:
            self.total_input_tokens += data['usage'].get('input_tokens', 0)
            self.total_output_tokens += data['usage'].get('output_tokens', 0)
            
        return data['content'][0]['text']

    def _invoke_converse(self, prompt: str, max_tokens: int, temperature: float,
                         model_id: str) -> str:
        resp = self.client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
        if 'usage' in resp:
            print("Bedrock Converse Usage data:", resp['usage'])
            self.total_input_tokens += resp['usage'].get('inputTokens', 0)
            self.total_output_tokens += resp['usage'].get('outputTokens', 0)
        return resp['output']['message']['content'][0]['text']

    def _invoke_bearer(self, prompt: str, max_tokens: int, temperature: float,
                       model_id: str) -> str:
        url = (
            f"https://bedrock-runtime.{Config.AWS_REGION}.amazonaws.com"
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
        
        # The bedrock converse API always includes usage struct
        print("Bedrock Bearer Raw Response Data Keys:", data.keys(), data.get('usage'))
        if 'usage' in data:
            self.total_input_tokens += data['usage'].get('inputTokens', 0)
            self.total_output_tokens += data['usage'].get('outputTokens', 0)
            
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
        raise ValueError(f"Could not parse JSON from LLM response:\n{text[:500]}")

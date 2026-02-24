import requests
import json
import os
from dotenv import load_dotenv

load_dotenv()

bearer_token = os.environ.get('AWS_BEARER_TOKEN_BEDROCK')
region = os.environ.get('AWS_REGION', 'us-east-1')
model_id = os.environ.get('BEDROCK_MODEL_ID', 'amazon.nova-2-lite-v1:0')

print(f"Token: {bearer_token[:10]}...")
print(f"Region: {region}")
print(f"Model ID: {model_id}")

url = f"https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse"
headers = {
    'Authorization': f'Bearer {bearer_token}',
    'Content-Type': 'application/json',
    'Accept': 'application/json',
}
payload = {
    "messages": [{"role": "user", "content": [{"text": "Hello, answer in one word."}]}],
    "inferenceConfig": {"maxTokens": 100, "temperature": 0.1},
}

try:
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    print("Status:", resp.status_code)
    try:
        data = resp.json()
        print("Keys:", data.keys())
        if 'usage' in data:
            print("Usage:", data['usage'])
        else:
            print("Full Data:", json.dumps(data, indent=2))
    except json.JSONDecodeError:
        print("Raw text:", resp.text)
except Exception as e:
    print("Error:", e)

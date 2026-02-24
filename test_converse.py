import sys
sys.path.append('/app')
from agents.bedrock_client import BedrockClient

def run():
    client = BedrockClient()
    prompt = "Hi, reply in 1 word."
    
    # Force Converse
    print("Testing Converse")
    resp = client._invoke_converse(prompt, max_tokens=10, temperature=0.1, model_id=client.model_id)
    print("Response text:", resp)
    print("Tokens Input:", client.total_input_tokens, "Tokens Output:", client.total_output_tokens)
    
    # Or bearer (whichever it's using)
    client.reset_tokens()
    print("Testing Generic Invoke")
    resp = client.invoke(prompt, max_tokens=10, temperature=0.1)
    print("Response text:", resp)
    print("Tokens Input:", client.total_input_tokens, "Tokens Output:", client.total_output_tokens)

if __name__ == "__main__":
    run()

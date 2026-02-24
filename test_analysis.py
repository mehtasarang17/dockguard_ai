import requests
import time

API_BASE = "http://localhost:5000"
HEADERS = {"X-Internal-Token": "docguard-internal-684d74ac-97a6"}

def run_test():
    # 2. Upload a simple test file
    with open("test.txt", "w") as f:
        f.write("This is a simple test policy document. All employees must follow security guidelines.")
    
    files = {"file": open("test.txt", "rb")}
    data = {"document_type": "policy"}
    print("Uploading test document...")
    resp = requests.post(f"{API_BASE}/api/upload", headers=HEADERS, files=files, data=data)
    if resp.status_code != 201:
        print("Upload failed:", resp.text)
        return
        
    doc_id = resp.json()["document_id"]
    print(f"Uploaded successfully. Document ID: {doc_id}")
    
    # 3. Poll for completion
    print("Waiting for analysis to complete...")
    for _ in range(30): # max 60s
        time.sleep(2)
        resp = requests.get(f"{API_BASE}/api/documents/{doc_id}", headers=HEADERS)
        if resp.status_code != 200:
            continue
            
        data = resp.json()
        status = data["document"]["status"]
        if status == "processing":
            continue
        elif status == "failed":
            print("Analysis failed.")
            return
        elif status == "completed":
            print("Analysis completed.")
            analysis = data.get("analysis", {})
            print("Tokens:", analysis.get("input_tokens"), "input, ", analysis.get("output_tokens"), "output,", analysis.get("total_tokens"), "total")
            return
            
    print("Timed out waiting for analysis.")

if __name__ == "__main__":
    run_test()

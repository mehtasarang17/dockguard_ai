import chromadb
from chromadb.config import Settings
client = chromadb.PersistentClient(path="./chroma_test", settings=Settings(anonymized_telemetry=False))
col = client.get_or_create_collection(name="test")
col.add(documents=["hello"], ids=["1"], metadatas=[{"a": "b"}])
print("Success!")

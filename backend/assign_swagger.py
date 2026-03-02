import re

with open('app.py', 'r') as f:
    text = f.read()

def inject(func_name, docstring):
    global text
    # Match standard docstring: def func_name(...):\n    """..."""
    pattern = r'(def ' + func_name + r'\([^)]*\):\n\s+)"""(.*?)"""'
    
    # We want to replace the old docstring with the new swagger one.
    # The replacement must keep the indentation.
    def repl(m):
        prefix = m.group(1)
        return prefix + '"""\n    ' + docstring.replace('\n', '\n    ') + '\n    """'
    text, n = re.subn(pattern, repl, text, flags=re.DOTALL)
    print(f"Injected {func_name}: {n} times")

# Health
inject("health", "Simple health check endpoint.\n---\ntags:\n  - Health\nresponses:\n  200:\n    description: Returns healthy status")

# Documents GET
inject("list_documents", "List all documents for the current tenant.\n---\ntags:\n  - Documents\nresponses:\n  200:\n    description: A list of documents\n    content:\n      application/json:\n        schema:\n          type: object\n          properties:\n            documents:\n              type: array\n              items:\n                $ref: '#/components/schemas/Document'")

# Document GET
inject("get_document", "Fetch metadata for a single document.\n---\ntags:\n  - Documents\nparameters:\n  - in: path\n    name: doc_id\n    required: true\n    schema:\n      type: integer\nresponses:\n  200:\n    description: Document details\n    content:\n      application/json:\n        schema:\n          type: object\n          properties:\n            document:\n              $ref: '#/components/schemas/Document'\n  404:\n    description: Document not found")

# Document POST
inject("upload_document", "Handle single document upload, extract text, and enqueue analysis.\n---\ntags:\n  - Documents\nrequestBody:\n  required: true\n  content:\n    multipart/form-data:\n      schema:\n        type: object\n        properties:\n          file:\n            type: string\n            format: binary\n          document_type:\n            type: string\n            enum: [policy, procedure, standard, guidelines]\n          llm_provider:\n            type: string\n          frameworks:\n            type: string\n          run_analysis:\n            type: string\n            enum: ['true', 'false']\nresponses:\n  201:\n    description: Document uploaded and processing")

# Batch POST
inject("upload_batch", "Upload multiple documents and start batch analysis.\n---\ntags:\n  - Batch\nrequestBody:\n  required: true\n  content:\n    multipart/form-data:\n      schema:\n        type: object\n        properties:\n          files:\n            type: array\n            items:\n              type: string\n              format: binary\n          document_types:\n            type: string\n          llm_provider:\n            type: string\n          frameworks:\n            type: string\nresponses:\n  201:\n    description: Batch processing started")

# Batch GET
inject("get_batch_analysis", "Check status and results of a batch analysis.\n---\ntags:\n  - Batch\nparameters:\n  - in: path\n    name: batch_id\n    required: true\n    schema:\n      type: integer\nresponses:\n  200:\n    description: Batch analysis status and results\n    content:\n      application/json:\n        schema:\n          $ref: '#/components/schemas/BatchAnalysis'\n  404:\n    description: Batch not found")

# Chat
inject("chat", "RAG chat answering questions based on vector store for the tenant.\n---\ntags:\n  - Chat\nrequestBody:\n  required: true\n  content:\n    application/json:\n      schema:\n        type: object\n        properties:\n          message:\n            type: string\n          llm_provider:\n            type: string\nresponses:\n  200:\n    description: Chat response\n    content:\n      application/json:\n        schema:\n          $ref: '#/components/schemas/ChatMessage'")

# Admin
inject("list_tenants", "Admin: List all tenants and their API keys.\n---\ntags:\n  - Admin\nresponses:\n  200:\n    description: List of tenants with keys\n  403:\n    description: Admin access required")

with open('app.py', 'w') as f:
    f.write(text)


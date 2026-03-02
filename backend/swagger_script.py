import re
import codecs

# This script will insert swagger YAML docstrings into app.py route functions

with codecs.open('app.py', 'r', 'utf-8') as f:
    app_py = f.read()

# We'll use simple replacements for the major routes to add swagger docs

replacements = [
    # /health
    (
        "@app.route('/health', methods=['GET'])\ndef health_check():\n    \"\"\"Simple health check endpoint.\"\"\"",
        "@app.route('/health', methods=['GET'])\ndef health_check():\n    \"\"\"\n    Simple health check endpoint.\n    ---\n    tags:\n      - Health\n    responses:\n      200:\n        description: Returns healthy status\n    \"\"\""
    ),
    # /api/documents (GET)
    (
        "@app.route('/api/documents', methods=['GET'])\ndef list_documents():\n    \"\"\"List all documents for the current tenant.\"\"\"",
        "@app.route('/api/documents', methods=['GET'])\ndef list_documents():\n    \"\"\"\n    List all documents for the current tenant.\n    ---\n    tags:\n      - Documents\n    responses:\n      200:\n        description: A list of documents\n        content:\n          application/json:\n            schema:\n              type: object\n              properties:\n                documents:\n                  type: array\n                  items:\n                    $ref: '#/components/schemas/Document'\n    \"\"\""
    ),
    # /api/documents/<id> (GET)
    (
        "@app.route('/api/documents/<int:doc_id>', methods=['GET'])\ndef get_document(doc_id):\n    \"\"\"Fetch metadata for a single document.\"\"\"",
        "@app.route('/api/documents/<int:doc_id>', methods=['GET'])\ndef get_document(doc_id):\n    \"\"\"\n    Fetch metadata for a single document.\n    ---\n    tags:\n      - Documents\n    parameters:\n      - in: path\n        name: doc_id\n        required: true\n        schema:\n          type: integer\n    responses:\n      200:\n        description: Document details\n        content:\n          application/json:\n            schema:\n              type: object\n              properties:\n                document:\n                  $ref: '#/components/schemas/Document'\n      404:\n        description: Document not found\n    \"\"\""
    ),
    # /api/upload
    (
        "@app.route('/api/upload', methods=['POST'])\ndef upload_document():\n    \"\"\"Handle single document upload, extract text, and enqueue analysis.\"\"\"",
        "@app.route('/api/upload', methods=['POST'])\ndef upload_document():\n    \"\"\"\n    Handle single document upload, extract text, and enqueue analysis.\n    ---\n    tags:\n      - Documents\n    requestBody:\n      required: true\n      content:\n        multipart/form-data:\n          schema:\n            type: object\n            properties:\n              file:\n                type: string\n                format: binary\n              document_type:\n                type: string\n                enum: [policy, procedure, standard, guidelines]\n              llm_provider:\n                type: string\n              frameworks:\n                type: string\n              run_analysis:\n                type: string\n                enum: ['true', 'false']\n    responses:\n      201:\n        description: Document uploaded and processing\n    \"\"\""
    ),
        # /api/upload-batch
    (
        "@app.route('/api/upload-batch', methods=['POST'])\ndef upload_batch():\n    \"\"\"Upload multiple documents and start batch analysis.\"\"\"",
        "@app.route('/api/upload-batch', methods=['POST'])\ndef upload_batch():\n    \"\"\"\n    Upload multiple documents and start batch analysis.\n    ---\n    tags:\n      - Batch\n    requestBody:\n      required: true\n      content:\n        multipart/form-data:\n          schema:\n            type: object\n            properties:\n              files:\n                type: array\n                items:\n                  type: string\n                  format: binary\n              document_types:\n                type: string\n              llm_provider:\n                type: string\n              frameworks:\n                type: string\n    responses:\n      201:\n        description: Batch processing started\n    \"\"\""
    ),
    # /api/batch-analysis/<id>
    (
        "@app.route('/api/batch-analysis/<int:batch_id>', methods=['GET'])\ndef get_batch_analysis(batch_id):\n    \"\"\"Check status and results of a batch analysis.\"\"\"",
        "@app.route('/api/batch-analysis/<int:batch_id>', methods=['GET'])\ndef get_batch_analysis(batch_id):\n    \"\"\"\n    Check status and results of a batch analysis.\n    ---\n    tags:\n      - Batch\n    parameters:\n      - in: path\n        name: batch_id\n        required: true\n        schema:\n          type: integer\n    responses:\n      200:\n        description: Batch analysis status and results\n        content:\n          application/json:\n            schema:\n              $ref: '#/components/schemas/BatchAnalysis'\n      404:\n        description: Batch not found\n    \"\"\""
    ),
    # /api/documents/<id> (DELETE)
    (
        "@app.route('/api/documents/<int:doc_id>', methods=['DELETE'])\ndef delete_document(doc_id):\n    \"\"\"Delete a document, its file, and its analysis.\"\"\"",
        "@app.route('/api/documents/<int:doc_id>', methods=['DELETE'])\ndef delete_document(doc_id):\n    \"\"\"\n    Delete a document, its file, and its analysis.\n    ---\n    tags:\n      - Documents\n    parameters:\n      - in: path\n        name: doc_id\n        required: true\n        schema:\n          type: integer\n    responses:\n      200:\n        description: Document deleted\n      404:\n        description: Document not found\n    \"\"\""
    ),
    # /api/documents/<id>/save
    (
        "@app.route('/api/documents/<int:doc_id>/save', methods=['POST'])\ndef save_document_to_kb(doc_id):\n    \"\"\"Chunk and save document text into the vector store.\"\"\"",
        "@app.route('/api/documents/<int:doc_id>/save', methods=['POST'])\ndef save_document_to_kb(doc_id):\n    \"\"\"\n    Chunk and save document text into the vector store.\n    ---\n    tags:\n      - Documents\n    parameters:\n      - in: path\n        name: doc_id\n        required: true\n        schema:\n          type: integer\n    responses:\n      200:\n        description: Document saved to Knowledge Base\n      404:\n        description: Document not found\n    \"\"\""
    ),
    # /api/analysis/<id>
    (
        "@app.route('/api/analysis/<int:doc_id>', methods=['GET'])\ndef get_analysis(doc_id):\n    \"\"\"Fetch analysis results for a document.\"\"\"",
        "@app.route('/api/analysis/<int:doc_id>', methods=['GET'])\ndef get_analysis(doc_id):\n    \"\"\"\n    Fetch analysis results for a document.\n    ---\n    tags:\n      - Analysis\n    parameters:\n      - in: path\n        name: doc_id\n        required: true\n        schema:\n          type: integer\n    responses:\n      200:\n        description: Analysis results\n        content:\n          application/json:\n            schema:\n              type: object\n              properties:\n                analysis:\n                  $ref: '#/components/schemas/Analysis'\n      404:\n        description: Analysis not found\n    \"\"\""
    ),
    # /api/chat
    (
        "@app.route('/api/chat', methods=['POST'])\ndef chat_with_docs():\n    \"\"\"RAG chat answering questions based on vector store for the tenant.\"\"\"",
        "@app.route('/api/chat', methods=['POST'])\ndef chat_with_docs():\n    \"\"\"\n    RAG chat answering questions based on vector store for the tenant.\n    ---\n    tags:\n      - Chat\n    requestBody:\n      required: true\n      content:\n        application/json:\n          schema:\n            type: object\n            properties:\n              message:\n                type: string\n              llm_provider:\n                type: string\n    responses:\n      200:\n        description: Chat response\n        content:\n          application/json:\n            schema:\n              $ref: '#/components/schemas/ChatMessage'\n    \"\"\""
    ),
    # /api/kb/stats
    (
        "@app.route('/api/kb/stats', methods=['GET'])\ndef kb_stats():\n    \"\"\"Return knowledge base status for the current tenant.\"\"\"",
        "@app.route('/api/kb/stats', methods=['GET'])\ndef kb_stats():\n    \"\"\"\n    Return knowledge base status for the current tenant.\n    ---\n    tags:\n      - Knowledge Base\n    responses:\n      200:\n        description: Knowledge Base stats\n        content:\n          application/json:\n            schema:\n              $ref: '#/components/schemas/KBStats'\n    \"\"\""
    ),
    # /api/frameworks
    (
        "@app.route('/api/frameworks', methods=['GET'])\ndef list_frameworks():\n    \"\"\"List all uploaded framework standards for the tenant.\"\"\"",
        "@app.route('/api/frameworks', methods=['GET'])\ndef list_frameworks():\n    \"\"\"\n    List all uploaded framework standards for the tenant.\n    ---\n    tags:\n      - Frameworks\n    responses:\n      200:\n        description: List of uploaded frameworks\n    \"\"\""
    ),
    # /api/trends
    (
        "@app.route('/api/trends', methods=['GET'])\ndef get_trends():\n    \"\"\"Get historical scores out of 100 for graph display.\"\"\"",
        "@app.route('/api/trends', methods=['GET'])\ndef get_trends():\n    \"\"\"\n    Get historical scores out of 100 for graph display.\n    ---\n    tags:\n      - History & Stats\n    responses:\n      200:\n        description: Trend data points\n    \"\"\""
    ),
    # /api/admin/tenants
    (
        "@app.route('/api/admin/tenants', methods=['GET'])\ndef list_tenants():\n    \"\"\"Admin: List all tenants and their API keys.\"\"\"",
        "@app.route('/api/admin/tenants', methods=['GET'])\ndef list_tenants():\n    \"\"\"\n    Admin: List all tenants and their API keys.\n    ---\n    tags:\n      - Admin\n    responses:\n      200:\n        description: List of tenants with keys\n      403:\n        description: Admin access required\n    \"\"\""
    )
]

for old, new in replacements:
    app_py = app_py.replace(old, new)

with codecs.open('app.py', 'w', 'utf-8') as f:
    f.write(app_py)

print("✅ Injected standard Swagger docstrings")

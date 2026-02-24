import os


class Config:
    # Database
    DATABASE_URL = os.environ.get(
        'DATABASE_URL',
        'postgresql://postgres:postgres@doc-analyzer-db:5432/document_analyzer'
    )

    # AWS Bedrock
    AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID', '')
    AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY', '')
    AWS_SESSION_TOKEN = os.environ.get('AWS_SESSION_TOKEN', '')
    AWS_BEARER_TOKEN_BEDROCK = os.environ.get('AWS_BEARER_TOKEN_BEDROCK', '')
    AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')
    BEDROCK_MODEL_ID = os.environ.get(
        'BEDROCK_MODEL_ID',
        'apac.amazon.nova-lite-v1:0'
    )
    # Fast/cheap model for structured extraction (compliance, security, risk, scoring)
    BEDROCK_MODEL_ID_FAST = os.environ.get(
        'BEDROCK_MODEL_ID_FAST',
        'apac.amazon.nova-lite-v1:0'
    )

    # Ollama (local LLM)
    OLLAMA_BASE_URL = os.environ.get('OLLAMA_BASE_URL', 'http://host.docker.internal:11434')
    OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'mistral:7b')

    # Upload settings
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', '/app/uploads')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB
    ALLOWED_EXTENSIONS = {'pdf', 'docx', 'doc', 'txt', 'xlsx', 'xls', 'csv'}

    # Vector Store
    CHROMADB_PATH = os.environ.get('CHROMADB_PATH', '/app/chromadb_data')

    # Flask
    DEBUG = os.environ.get('DEBUG', 'False').lower() in ('true', '1', 'yes')
    SECRET_KEY = os.environ.get('SECRET_KEY', 'doc-analyzer-secret-key-change-me')

    # Internal token shared between Nginx proxy and Flask for frontend bypass
    INTERNAL_TOKEN = os.environ.get('INTERNAL_TOKEN', 'docguard-internal-684d74ac-97a6')

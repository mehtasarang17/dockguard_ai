from sqlalchemy import (
    create_engine, Column, Integer, String, Text, Float,
    DateTime, Boolean, ForeignKey, JSON, text, Index, UniqueConstraint
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from pgvector.sqlalchemy import Vector
from datetime import datetime
from config import Config

# ==============================================================================
# Two declarative bases — one for the central/admin DB, one for per-tenant DBs.
#
# CentralBase  → tables that live ONLY in the central database
#                 (tenants, authorized_apps, system_settings)
# TenantBase   → tables that live in EACH tenant's own database
#                 (documents, analyses, kb_chunks, etc.)
#
# For the default tenant (id=1), tenant tables also exist in the central DB
# (backward compatibility — no migration needed).
# ==============================================================================

CentralBase = declarative_base()
TenantBase = declarative_base()


# ==============================================================================
# CENTRAL MODELS — stored in the admin/auth database
# ==============================================================================

class Tenant(CentralBase):
    """A tenant is the top-level isolation unit. Every API key belongs to one tenant."""
    __tablename__ = 'tenants'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    slug = Column(String(100), unique=True, nullable=False)   # URL-safe identifier
    db_name = Column(String(100), nullable=True)              # PostgreSQL database name for this tenant
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Per-tenant LLM Configuration (encrypted bearer token)
    llm_aws_bearer_token = Column(Text, nullable=True)        # Encrypted
    llm_aws_region = Column(String(50), nullable=True)        # e.g., 'us-east-1'
    llm_bedrock_model_id = Column(String(100), nullable=True)
    llm_config_updated_at = Column(DateTime, nullable=True)

    # Relationships (central DB only)
    authorized_apps = relationship("AuthorizedApp", back_populates="tenant", cascade="all, delete-orphan")

    @property
    def has_llm_config(self):
        """Check if tenant has LLM configuration set."""
        return bool(self.llm_aws_bearer_token and self.llm_aws_region)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'slug': self.slug,
            'db_name': self.db_name,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'has_llm_config': self.has_llm_config,
        }


class AuthorizedApp(CentralBase):
    """Registered application with its own API key. Belongs to a Tenant."""
    __tablename__ = 'authorized_apps'

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False, default=1)
    name = Column(String(100), nullable=False)

    # --- Hashed credentials ---
    api_key_hash = Column(String(64), unique=True, nullable=True)      # SHA-256 hex digest
    api_key_prefix = Column(String(12), nullable=True)                 # e.g. "sk-550e84" for display
    refresh_token_hash = Column(String(64), unique=True, nullable=True) # SHA-256 hex digest
    refresh_token_expires_at = Column(DateTime, nullable=True)           # NULL = no active refresh token

    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)  # admin keys can manage tenants
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)       # NULL = never expires

    # Relationship
    tenant = relationship("Tenant", back_populates="authorized_apps")

    @property
    def is_expired(self):
        return self.expires_at is not None and self.expires_at < datetime.utcnow()

    def to_dict(self):
        return {
            'id': self.id,
            'tenant_id': self.tenant_id,
            'name': self.name,
            'api_key_prefix': self.api_key_prefix,
            'is_active': self.is_active,
            'is_admin': self.is_admin,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_used': self.last_used.isoformat() if self.last_used else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'is_expired': self.is_expired,
        }


class SystemSettings(CentralBase):
    __tablename__ = 'system_settings'

    id = Column(Integer, primary_key=True)
    key = Column(String(50), unique=True, nullable=False)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'key': self.key,
            'value': self.value,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


# ==============================================================================
# TENANT MODELS — stored in each tenant's own database
# ==============================================================================

class Document(TenantBase):
    __tablename__ = 'documents'

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False, default=1)   # kept for traceability
    filename = Column(String(255), nullable=False)
    original_filename = Column(String(255), nullable=False)
    file_path = Column(String(512), nullable=False)
    document_type = Column(String(50), nullable=False)  # policy, contract, procedure
    file_size = Column(Integer, default=0)
    upload_date = Column(DateTime, default=datetime.utcnow)
    status = Column(String(50), default='uploaded')  # uploaded, processing, completed, failed
    is_saved = Column(Boolean, default=False)  # user explicitly saved for knowledge base
    markdown_text = Column(Text, nullable=True)  # cached Markdown conversion of the original file

    # Relationships (within tenant DB)
    analysis = relationship("Analysis", back_populates="document", uselist=False, cascade="all, delete-orphan")
    chat_messages = relationship("ChatHistory", back_populates="document", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            'id': self.id,
            'filename': self.original_filename,
            'document_type': self.document_type,
            'file_size': self.file_size,
            'upload_date': self.upload_date.isoformat() if self.upload_date else None,
            'status': self.status,
            'is_saved': self.is_saved,
            'has_analysis': self.analysis is not None,
        }


class Analysis(TenantBase):
    __tablename__ = 'analyses'

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey('documents.id', ondelete='CASCADE'), nullable=False, unique=True)

    # --- Core Scores ---
    compliance_score = Column(Float, default=0)
    security_score = Column(Float, default=0)
    risk_score = Column(Float, default=0)
    overall_score = Column(Float, default=0)

    # --- Detailed Scores ---
    completeness_score = Column(Float, default=0)
    security_strength_score = Column(Float, default=0)
    coverage_score = Column(Float, default=0)
    clarity_score = Column(Float, default=0)
    enforcement_score = Column(Float, default=0)

    # --- Findings (JSON blobs) ---
    compliance_findings = Column(JSON, default=list)
    security_findings = Column(JSON, default=list)
    risk_findings = Column(JSON, default=list)

    # --- Framework Mapping ---
    framework_mappings = Column(JSON, default=dict)
    # structure: { "ISO27001": {...}, "SOC2": {...}, "NIST": {...}, "CIS": {...}, "GDPR": {...}, "HIPAA": {...} }

    # --- Gap Detection ---
    gap_detections = Column(JSON, default=list)
    # list of { "gap_type": "missing_encryption_policy", "detected": true, "details": "...", "severity": "high" }

    # --- Best Practices ---
    best_practices = Column(JSON, default=list)

    # --- Auto-Suggestions ---
    suggestions = Column(JSON, default=list)
    # list of { "type": "policy_improvement|missing_clause|wording|security", "suggestion": "...", "priority": "..." }

    # --- Risk & Maturity ---
    risk_level = Column(String(20), default='medium')  # low, medium, high, critical
    document_maturity = Column(String(20), default='basic')  # basic, developing, established, mature, optimized

    # --- Recommendations ---
    recommendations = Column(JSON, default=list)

    # --- Score Rationale ---
    score_rationale = Column(JSON, default=list)

    # --- Tokens ---
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)

    # --- Meta ---
    analysis_date = Column(DateTime, default=datetime.utcnow)
    processing_time = Column(Float)

    # Relationship
    document = relationship("Document", back_populates="analysis")

    def to_dict(self):
        return {
            'id': self.id,
            'document_id': self.document_id,
            # Core scores
            'compliance_score': self.compliance_score,
            'security_score': self.security_score,
            'risk_score': self.risk_score,
            'overall_score': self.overall_score,
            # Detailed scores
            'completeness_score': self.completeness_score,
            'security_strength_score': self.security_strength_score,
            'coverage_score': self.coverage_score,
            'clarity_score': self.clarity_score,
            'enforcement_score': self.enforcement_score,
            # Findings
            'compliance_findings': self.compliance_findings or [],
            'security_findings': self.security_findings or [],
            'risk_findings': self.risk_findings or [],
            # Framework mapping
            'framework_mappings': self.framework_mappings or {},
            # Gap detection
            'gap_detections': self.gap_detections or [],
            # Best practices
            'best_practices': self.best_practices or [],
            # Auto-suggestions
            'suggestions': self.suggestions or [],
            # Risk & maturity
            'risk_level': self.risk_level,
            'document_maturity': self.document_maturity,
            # Recommendations
            'recommendations': self.recommendations or [],
            'score_rationale': self.score_rationale or [],
            # Tokens
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'total_tokens': self.total_tokens,
            # Meta
            'analysis_date': self.analysis_date.isoformat() if self.analysis_date else None,
            'processing_time': self.processing_time,
        }


class ChatHistory(TenantBase):
    __tablename__ = 'chat_history'

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False, default=1)   # kept for traceability
    document_id = Column(Integer, ForeignKey('documents.id', ondelete='CASCADE'), nullable=True)
    role = Column(String(20), nullable=False)  # user, assistant
    message = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    tokens_used = Column(Integer, default=0)

    document = relationship("Document", back_populates="chat_messages", foreign_keys=[document_id])

    def to_dict(self):
        return {
            'id': self.id,
            'document_id': self.document_id,
            'role': self.role,
            'message': self.message,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'tokens_used': self.tokens_used,
        }


class BatchAnalysis(TenantBase):
    """Stores results of multi-document batch analysis."""
    __tablename__ = 'batch_analyses'

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False, default=1)   # kept for traceability
    # document_ids (JSON) removed — replaced by BatchDocument pivot table
    document_type = Column(String(50), nullable=False)
    status = Column(String(50), default='processing')   # processing, completed, failed

    # --- Unified Scores ---
    overall_score = Column(Float, default=0)
    risk_level = Column(String(20), default='medium')
    document_maturity = Column(String(20), default='developing')

    # --- Cross-Document Analysis (JSON blobs) ---
    cross_doc_gaps = Column(JSON, default=dict)         # resolved_gaps, corpus_gaps, contradictions
    synthesis = Column(JSON, default=dict)              # coverage_summary, top_priorities, strengths
    recommendations = Column(JSON, default=list)
    score_rationale = Column(JSON, default=list)

    # --- Meta ---
    created_at = Column(DateTime, default=datetime.utcnow)
    processing_time = Column(Float)

    # Pivot relationship
    batch_documents = relationship("BatchDocument", cascade="all, delete-orphan", lazy="joined")

    @property
    def document_ids(self):
        """Return ordered list of document IDs via the pivot table."""
        return [bd.document_id for bd in (self.batch_documents or [])]

    def to_dict(self):
        return {
            'id': self.id,
            'document_ids': self.document_ids,
            'document_type': self.document_type,
            'status': self.status,
            'overall_score': self.overall_score,
            'risk_level': self.risk_level,
            'document_maturity': self.document_maturity,
            'cross_doc_gaps': self.cross_doc_gaps or {},
            'synthesis': self.synthesis or {},
            'recommendations': self.recommendations or [],
            'score_rationale': self.score_rationale or [],
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'processing_time': self.processing_time,
        }


class BatchDocument(TenantBase):
    """Pivot table — links a BatchAnalysis to its constituent Documents."""
    __tablename__ = 'batch_documents'

    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, ForeignKey('batch_analyses.id', ondelete='CASCADE'), nullable=False)
    document_id = Column(Integer, ForeignKey('documents.id', ondelete='CASCADE'), nullable=False)

    __table_args__ = (
        UniqueConstraint('batch_id', 'document_id', name='uq_batch_document'),
        Index('ix_batch_documents_batch_id', 'batch_id'),
        Index('ix_batch_documents_document_id', 'document_id'),
    )


# ---- pgvector Tables (Tenant DB) --------------------------------------------

class KBChunk(TenantBase):
    """Knowledge Base chunk with vector embedding (per-tenant DB)."""
    __tablename__ = 'kb_chunks'

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)          # kept for traceability
    doc_id = Column(Integer, ForeignKey('documents.id', ondelete='CASCADE'), nullable=False)
    filename = Column(String(255))
    chunk_index = Column(Integer, default=0)
    content = Column(Text, nullable=False)
    embedding = Column(Vector(384))  # all-MiniLM-L6-v2 = 384 dims

    __table_args__ = (
        Index('ix_kb_chunks_tenant_doc', 'tenant_id', 'doc_id'),
    )


class FrameworkChunk(TenantBase):
    """Framework standard chunk with vector embedding (per-tenant DB)."""
    __tablename__ = 'framework_chunks'

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)          # kept for traceability
    framework_key = Column(String(20), nullable=False)
    version = Column(String(100))
    filename = Column(String(255))
    chunk_index = Column(Integer, default=0)
    content = Column(Text, nullable=False)
    embedding = Column(Vector(384))

    __table_args__ = (
        Index('ix_fw_chunks_tenant_key', 'tenant_id', 'framework_key'),
    )


class ChatFileChunk(TenantBase):
    """Temporary chat file chunk with vector embedding."""
    __tablename__ = 'chat_file_chunks'

    id = Column(Integer, primary_key=True)
    session_id = Column(String(100), nullable=False, index=True)
    filename = Column(String(255))
    chunk_index = Column(Integer, default=0)
    content = Column(Text, nullable=False)
    embedding = Column(Vector(384))
    created_at = Column(DateTime, default=datetime.utcnow)


class FrameworkStandard(TenantBase):
    """Tracks uploaded compliance framework standard documents (per-tenant DB)."""
    __tablename__ = 'framework_standards'

    VALID_KEYS = ('CIS', 'GDPR', 'HIPAA', 'ISO27001', 'NIST', 'SOC2')

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False, default=1)   # kept for traceability
    framework_key = Column(String(20), nullable=False)   # e.g. ISO27001
    version = Column(String(100), nullable=False)         # e.g. "2022"
    filename = Column(String(255), nullable=False)        # original filename
    file_path = Column(String(500), nullable=False)       # server-side path
    chunk_count = Column(Integer, default=0)
    uploaded_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'framework_key': self.framework_key,
            'version': self.version,
            'filename': self.filename,
            'chunk_count': self.chunk_count,
            'uploaded_at': self.uploaded_at.isoformat() if self.uploaded_at else None,
        }


# ==============================================================================
# DATABASE HELPERS
# ==============================================================================

from sqlalchemy.pool import NullPool

# Central DB engine — used for auth/admin operations
engine = create_engine(Config.DATABASE_URL, pool_pre_ping=True, poolclass=NullPool)
CentralSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Backward-compat alias: SessionLocal points to central DB
# (used ONLY for imports that still reference it during migration)
SessionLocal = CentralSessionLocal


def init_db():
    """Initialise the central database (admin/auth tables).

    Also creates tenant tables in the central DB for backward compatibility
    with the default tenant (id=1).
    """
    # Enable pgvector extension
    with engine.connect() as conn:
        conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        conn.commit()

    # Create central tables
    CentralBase.metadata.create_all(bind=engine)

    # Create tenant tables in the central DB (for the default tenant)
    TenantBase.metadata.create_all(bind=engine)

    # Create HNSW indexes for vector search (idempotent)
    hnsw_indexes = [
        'CREATE INDEX IF NOT EXISTS ix_kb_chunks_embedding ON kb_chunks USING hnsw (embedding vector_cosine_ops)',
        'CREATE INDEX IF NOT EXISTS ix_framework_chunks_embedding ON framework_chunks USING hnsw (embedding vector_cosine_ops)',
        'CREATE INDEX IF NOT EXISTS ix_chat_file_chunks_embedding ON chat_file_chunks USING hnsw (embedding vector_cosine_ops)',
    ]
    with engine.connect() as conn:
        for stmt in hnsw_indexes:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()

    # ---- Migrations (safe, idempotent) ----------------------------------------
    migrations = [
        # Multi-tenancy: add tenant_id columns (default to tenant 1)
        'ALTER TABLE documents ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1',
        'ALTER TABLE batch_analyses ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1',
        'ALTER TABLE chat_history ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1',
        'ALTER TABLE framework_standards ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1',
        # AuthorizedApp: add tenant_id + is_admin
        'ALTER TABLE authorized_apps ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1',
        'ALTER TABLE authorized_apps ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE',
        # API key expiration & refresh tokens
        'ALTER TABLE authorized_apps ADD COLUMN expires_at TIMESTAMP',
        'ALTER TABLE authorized_apps ADD COLUMN refresh_token VARCHAR(100) UNIQUE',
        # Hash-only API key storage
        'ALTER TABLE authorized_apps ADD COLUMN api_key_hash VARCHAR(64) UNIQUE',
        'ALTER TABLE authorized_apps ADD COLUMN api_key_prefix VARCHAR(12)',
        'ALTER TABLE authorized_apps ADD COLUMN refresh_token_hash VARCHAR(64) UNIQUE',
        # Make legacy plaintext columns nullable (for transition)
        'ALTER TABLE authorized_apps ALTER COLUMN api_key DROP NOT NULL',
        # Markdown text cache
        'ALTER TABLE documents ADD COLUMN markdown_text TEXT',
        # Older app migrations
        'ALTER TABLE chat_history ALTER COLUMN document_id DROP NOT NULL',
        'ALTER TABLE chat_history ADD COLUMN tokens_used INTEGER DEFAULT 0',
        'ALTER TABLE analyses ADD COLUMN score_rationale JSON DEFAULT \'[]\'',
        'ALTER TABLE batch_analyses ADD COLUMN score_rationale JSON DEFAULT \'[]\'',
        # Database-per-tenant: add db_name to tenants
        'ALTER TABLE tenants ADD COLUMN db_name VARCHAR(100)',
        # Per-tenant LLM configuration
        'ALTER TABLE tenants ADD COLUMN llm_aws_bearer_token TEXT',
        'ALTER TABLE tenants ADD COLUMN llm_aws_region VARCHAR(50)',
        'ALTER TABLE tenants ADD COLUMN llm_bedrock_model_id VARCHAR(100)',
        'ALTER TABLE tenants ADD COLUMN llm_config_updated_at TIMESTAMP',
        # Refresh token expiration (short-lived refresh tokens)
        'ALTER TABLE authorized_apps ADD COLUMN refresh_token_expires_at TIMESTAMP',
        # Pivot table migration: make old JSON column nullable before dropping mapping
        'ALTER TABLE batch_analyses ALTER COLUMN document_ids DROP NOT NULL',
    ]
    with engine.connect() as conn:
        for stmt in migrations:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()  # column / constraint already exists — safe to ignore

    # Batch documents pivot table + data migration (central DB / default tenant)
    _run_batch_pivot_migrations(engine)

    # Backfill hashed API keys from existing plaintext values
    _backfill_api_key_hashes(engine)

    # Drop legacy plaintext columns (safe — backfill already ran)
    _drop_legacy_plaintext_columns(engine)


def _run_batch_pivot_migrations(engine):
    """Create batch_documents pivot table and migrate existing JSON data.

    Safe to run multiple times — all statements are idempotent.
    """
    stmts = [
        # 1. Create the pivot table if it doesn't exist
        """
        CREATE TABLE IF NOT EXISTS batch_documents (
            id          SERIAL PRIMARY KEY,
            batch_id    INTEGER NOT NULL
                            REFERENCES batch_analyses(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL
                            REFERENCES documents(id)      ON DELETE CASCADE,
            CONSTRAINT uq_batch_document UNIQUE (batch_id, document_id)
        )
        """,
        # 2. Indexes for fast lookups in both directions
        'CREATE INDEX IF NOT EXISTS ix_batch_documents_batch_id    ON batch_documents(batch_id)',
        'CREATE INDEX IF NOT EXISTS ix_batch_documents_document_id ON batch_documents(document_id)',
        # 3. Migrate existing JSON array data → pivot rows (idempotent via ON CONFLICT DO NOTHING)
        """
        INSERT INTO batch_documents (batch_id, document_id)
        SELECT ba.id, (elem.value::text)::integer
        FROM   batch_analyses ba,
               json_array_elements(ba.document_ids) AS elem(value)
        WHERE  ba.document_ids IS NOT NULL
          AND  ba.document_ids::text NOT IN ('null', '[]')
        ON CONFLICT DO NOTHING
        """,
    ]
    with engine.connect() as conn:
        for stmt in stmts:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()


def _backfill_api_key_hashes(engine):
    """Backfill api_key_hash, api_key_prefix, and refresh_token_hash from existing plaintext values.

    Safe to run multiple times — only updates rows that have plaintext but no hash.
    Uses Python-side hashing (SHA-256) since PostgreSQL doesn't have a built-in SHA-256 text function.
    """
    import hashlib

    with engine.connect() as conn:
        try:
            rows = conn.execute(text(
                "SELECT id, api_key, refresh_token FROM authorized_apps "
                "WHERE api_key IS NOT NULL AND api_key_hash IS NULL"
            )).fetchall()

            for row in rows:
                row_id, api_key, refresh_token = row[0], row[1], row[2]
                api_key_hash = hashlib.sha256(api_key.encode()).hexdigest() if api_key else None
                api_key_prefix = api_key[:10] if api_key else None
                refresh_token_hash = hashlib.sha256(refresh_token.encode()).hexdigest() if refresh_token else None

                conn.execute(text(
                    "UPDATE authorized_apps SET api_key_hash = :akh, api_key_prefix = :akp, "
                    "refresh_token_hash = :rth WHERE id = :id"
                ), {'akh': api_key_hash, 'akp': api_key_prefix, 'rth': refresh_token_hash, 'id': row_id})

            conn.commit()
            if rows:
                print(f"🔒 Backfilled hashes for {len(rows)} existing API key(s)")
        except Exception as e:
            conn.rollback()
            print(f"⚠️  API key hash backfill: {e}")


def _drop_legacy_plaintext_columns(engine):
    """Drop the legacy plaintext api_key and refresh_token columns.

    Safe to run multiple times — DROP IF EXISTS is idempotent.
    Must only run AFTER _backfill_api_key_hashes() has completed.
    """
    drop_stmts = [
        'ALTER TABLE authorized_apps DROP COLUMN IF EXISTS api_key',
        'ALTER TABLE authorized_apps DROP COLUMN IF EXISTS refresh_token',
    ]
    with engine.connect() as conn:
        for stmt in drop_stmts:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()
    print("🗑️  Dropped legacy plaintext api_key / refresh_token columns")


def get_central_db():
    """Return a session for the central/admin database."""
    return CentralSessionLocal()


def get_db():
    """Backward-compat alias for get_central_db().

    Prefer get_central_db() for admin/auth operations and
    tenant_db.get_tenant_session(db_name) for tenant-scoped operations.
    """
    return CentralSessionLocal()

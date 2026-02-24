from sqlalchemy import (
    create_engine, Column, Integer, String, Text, Float,
    DateTime, Boolean, ForeignKey, JSON, text
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
from config import Config

Base = declarative_base()


class Document(Base):
    __tablename__ = 'documents'

    id = Column(Integer, primary_key=True)
    filename = Column(String(255), nullable=False)
    original_filename = Column(String(255), nullable=False)
    file_path = Column(String(512), nullable=False)
    document_type = Column(String(50), nullable=False)  # policy, contract, procedure
    file_size = Column(Integer, default=0)
    upload_date = Column(DateTime, default=datetime.utcnow)
    status = Column(String(50), default='uploaded')  # uploaded, processing, completed, failed
    is_saved = Column(Boolean, default=False)  # user explicitly saved for knowledge base

    # Relationships
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


class Analysis(Base):
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


class ChatHistory(Base):
    __tablename__ = 'chat_history'

    id = Column(Integer, primary_key=True)
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


class BatchAnalysis(Base):
    """Stores results of multi-document batch analysis."""
    __tablename__ = 'batch_analyses'

    id = Column(Integer, primary_key=True)
    document_ids = Column(JSON, nullable=False)         # list of document IDs in this batch
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

    def to_dict(self):
        return {
            'id': self.id,
            'document_ids': self.document_ids or [],
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


# ---- Database Helpers ----
from sqlalchemy.pool import NullPool
engine = create_engine(Config.DATABASE_URL, pool_pre_ping=True, poolclass=NullPool)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    Base.metadata.create_all(bind=engine)
    # Migrate: make chat_history.document_id nullable if it isn't already
    try:
        with engine.connect() as conn:
            conn.execute(text('ALTER TABLE chat_history ALTER COLUMN document_id DROP NOT NULL'))
            conn.execute(text('ALTER TABLE chat_history ADD COLUMN tokens_used INTEGER DEFAULT 0'))
            conn.commit()
    except Exception:
        pass  # already nullable or table doesn't exist yet
    # Migrate: add score_rationale columns
    try:
        with engine.connect() as conn:
            conn.execute(text('ALTER TABLE analyses ADD COLUMN score_rationale JSON DEFAULT \'[]\' '))
            conn.execute(text('ALTER TABLE batch_analyses ADD COLUMN score_rationale JSON DEFAULT \'[]\' '))
            conn.commit()
    except Exception:
        pass  # columns already exist


class FrameworkStandard(Base):
    """Tracks uploaded compliance framework standard documents."""
    __tablename__ = 'framework_standards'

    VALID_KEYS = ('CIS', 'GDPR', 'HIPAA', 'ISO27001', 'NIST', 'SOC2')

    id = Column(Integer, primary_key=True)
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


class AuthorizedApp(Base):
    """Registered application with its own API key for external access."""
    __tablename__ = 'authorized_apps'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    api_key = Column(String(100), unique=True, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used = Column(DateTime, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'api_key': self.api_key,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_used': self.last_used.isoformat() if self.last_used else None,
        }


class SystemSettings(Base):
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


def get_db():
    return SessionLocal()

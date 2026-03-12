"""
Tenant Database Manager — per-tenant database lifecycle for HIPAA-grade isolation.

Each compliance tenant gets its own PostgreSQL database.
The central/admin database (Config.DATABASE_URL) stores tenant metadata and auth
credentials.  This module manages:

  * Engine + session caching per tenant (thread-safe)
  * Creating / dropping tenant databases
  * Initialising the tenant schema (pgvector + all tenant tables + HNSW indexes)
"""

import threading
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from config import Config


# ---- Engine cache (thread-safe) ---------------------------------------------
_tenant_engines: dict[str, tuple] = {}   # db_name -> (engine, SessionMaker)
_lock = threading.Lock()


def _base_db_url() -> str:
    """Return the DB URL prefix (without database name) for constructing tenant URLs.

    Example: 'postgresql://user:pass@host:5432/document_analyzer'
              → 'postgresql://user:pass@host:5432'
    """
    return Config.DATABASE_URL.rsplit('/', 1)[0]


def get_tenant_db_url(db_name: str) -> str:
    """Construct a full database URL for *db_name*."""
    return f"{_base_db_url()}/{db_name}"


# ---- Session factory --------------------------------------------------------

def get_tenant_session(db_name: str):
    """Return a **new** SQLAlchemy session connected to the tenant's database.

    Engines are lazily created and cached so that repeated requests for the
    same tenant reuse the same pool.  On first creation the engine also runs
    incremental schema migrations so existing tenant DBs stay up to date.
    """
    with _lock:
        if db_name not in _tenant_engines:
            url = get_tenant_db_url(db_name)
            engine = create_engine(url, pool_pre_ping=True, poolclass=NullPool)
            factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            _tenant_engines[db_name] = (engine, factory)
            # Run incremental migrations for this tenant DB (idempotent)
            _run_tenant_migrations(engine)

    _, factory = _tenant_engines[db_name]
    return factory()


def _run_tenant_migrations(engine):
    """Apply incremental schema migrations to an existing tenant database.

    Called once per process lifetime when a tenant engine is first created.
    All statements are idempotent — safe to re-run.
    """
    from models import _run_batch_pivot_migrations  # deferred to avoid circular import

    stmts = [
        # Pivot table migration: make old JSON column nullable
        'ALTER TABLE batch_analyses ALTER COLUMN document_ids DROP NOT NULL',
    ]
    with engine.connect() as conn:
        for stmt in stmts:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()

    _run_batch_pivot_migrations(engine)


# ---- Database lifecycle -----------------------------------------------------

def create_tenant_database(slug: str) -> str:
    """Create a new PostgreSQL database for a tenant and initialise its schema.

    Steps:
        1. CREATE DATABASE docguard_<slug>  (idempotent)
        2. CREATE EXTENSION IF NOT EXISTS vector
        3. Create all TenantBase tables
        4. Build HNSW indexes for vector search

    Returns the database name (e.g. 'docguard_acme_corp').
    """
    from models import TenantBase  # deferred to avoid circular import

    db_name = f"docguard_{slug.replace('-', '_')}"

    # --- 1. Create the database via the central connection ---
    central_engine = create_engine(Config.DATABASE_URL, poolclass=NullPool)
    with central_engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": db_name},
        ).first()
        if not exists:
            # Use TEMPLATE template0 to avoid collation version mismatch errors
            conn.execute(text(f'CREATE DATABASE "{db_name}" TEMPLATE template0'))
            print(f"🗄️  Created PostgreSQL database: {db_name}")
    central_engine.dispose()

    # --- 2-4. Initialise the schema ---
    _init_tenant_schema(db_name, TenantBase)

    return db_name


def _init_tenant_schema(db_name: str, tenant_base):
    """Enable pgvector, create tables, and build HNSW indexes in a tenant DB."""
    tenant_url = get_tenant_db_url(db_name)
    engine = create_engine(tenant_url, poolclass=NullPool)

    # Enable pgvector extension
    with engine.connect() as conn:
        conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        conn.commit()

    # Create all tenant tables
    tenant_base.metadata.create_all(bind=engine)

    # Build HNSW indexes (idempotent)
    hnsw_indexes = [
        'CREATE INDEX IF NOT EXISTS ix_kb_chunks_embedding '
        'ON kb_chunks USING hnsw (embedding vector_cosine_ops)',
        'CREATE INDEX IF NOT EXISTS ix_framework_chunks_embedding '
        'ON framework_chunks USING hnsw (embedding vector_cosine_ops)',
        'CREATE INDEX IF NOT EXISTS ix_chat_file_chunks_embedding '
        'ON chat_file_chunks USING hnsw (embedding vector_cosine_ops)',
    ]
    with engine.connect() as conn:
        for stmt in hnsw_indexes:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()

    # New tenant DBs don't have the old JSON column, so no nullable migration needed,
    # but run the pivot helper so the table + indexes are always created.
    from models import _run_batch_pivot_migrations
    _run_batch_pivot_migrations(engine)

    engine.dispose()
    print(f"📋 Tenant schema initialised for database: {db_name}")


def drop_tenant_database(db_name: str):
    """Drop a tenant database.  **USE WITH EXTREME CAUTION.**

    Terminates active connections and drops the database.
    """
    if db_name == Config.DATABASE_URL.rsplit('/', 1)[1]:
        raise ValueError("Cannot drop the central database!")

    # Evict from cache
    with _lock:
        cached = _tenant_engines.pop(db_name, None)
        if cached:
            cached[0].dispose()

    central_engine = create_engine(Config.DATABASE_URL, poolclass=NullPool)
    with central_engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        # Terminate active connections
        conn.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = :name AND pid <> pg_backend_pid()"
        ), {"name": db_name})
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    central_engine.dispose()
    print(f"🗑️  Dropped tenant database: {db_name}")

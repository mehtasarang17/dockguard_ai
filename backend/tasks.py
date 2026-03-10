"""
Celery task definitions for document analysis.

These are thin wrappers around the processing functions in processing.py.
They handle retry logic and error reporting for the task queue.

Each task receives a db_name parameter to route to the correct tenant database.
"""
from celery_app import celery_app
from processing import process_document, process_batch


@celery_app.task(
    name='tasks.process_document',
    bind=True,
    max_retries=2,
    default_retry_delay=10,
    acks_late=True,
)
def process_document_task(self, db_name: str, document_id: int, file_path: str, document_type: str):
    """Celery task: analyse a single document.

    Retries up to 2 times on transient errors (e.g. Bedrock throttling).
    """
    try:
        print(f"📥 [Celery] Starting document analysis: doc_id={document_id}, db={db_name}")
        process_document(db_name, document_id, file_path, document_type)
        print(f"📤 [Celery] Completed document analysis: doc_id={document_id}")
    except Exception as exc:
        print(f"⚠️  [Celery] Document {document_id} failed (attempt {self.request.retries + 1}): {exc}")
        # Retry on transient errors
        raise self.retry(exc=exc)


@celery_app.task(
    name='tasks.process_batch',
    bind=True,
    max_retries=1,
    default_retry_delay=30,
    acks_late=True,
)
def process_batch_task(self, db_name: str, batch_id: int, documents_info: list, document_type: str):
    """Celery task: analyse a batch of documents with cross-doc synthesis.

    Retries once on transient errors. Individual document failures within
    the batch are handled gracefully by the Orchestrator.
    """
    try:
        print(f"📥 [Celery] Starting batch analysis: batch_id={batch_id}, db={db_name}, {len(documents_info)} documents")
        process_batch(db_name, batch_id, documents_info, document_type)
        print(f"📤 [Celery] Completed batch analysis: batch_id={batch_id}")
    except Exception as exc:
        print(f"⚠️  [Celery] Batch {batch_id} failed (attempt {self.request.retries + 1}): {exc}")
        raise self.retry(exc=exc)
